# =========================
# PASS 4: Optional GitHub Actions job-log enrichment (last 60 days)
#
# Purpose:
#   - Download logs for *instrumentation-related* jobs only (as inferred from Pass 2 steps + V16 patterns)
#   - Extract log-derived features using the SAME V16 pattern sets (incl. Gradle GMD detection)
#   - Keep logs optional: this pass should never be required for performance metrics (Pass 3).
#
# Inputs:
#   out/workflow_catalog_v16.csv                 (Pass 1)
#   out/pass2_recent_60d/runs_recent.csv         (Pass 2)
#   out/pass2_recent_60d/jobs_recent.csv         (Pass 2)
#   out/pass2_recent_60d/steps_recent.csv        (Pass 2)
#   out/pass3_metrics_60d/run_metrics.csv        (Pass 3) [optional; used only for prioritization]
#
# Outputs:
#   out/pass4_log_enrichment_60d/job_log_features.csv
#
# Notes:
#   - GitHub's job logs endpoint returns a 302 redirect to a short-lived download URL (typically plain text).
#   - This pass streams logs with a size cap to avoid huge outputs.
# =========================

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import io
import os
from pathlib import Path
import threading
import time
import zipfile
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

# -----
# Project paths (assumes this script is next to other passes)
# -----
BASE_DIR = Path(__file__).resolve().parent

P1_DIR = BASE_DIR / "out"
IN_CATALOG = P1_DIR / "workflow_catalog_v16.csv"

P2_DIR = BASE_DIR / "out" / "pass2_recent_60d"
IN_RUNS  = P2_DIR / "runs_recent.csv"
IN_JOBS  = P2_DIR / "jobs_recent.csv"
IN_STEPS = P2_DIR / "steps_recent.csv"

P3_DIR = BASE_DIR / "out" / "pass3_metrics_60d"
IN_RUN_MET = P3_DIR / "run_metrics.csv"  # optional

OUT_DIR = BASE_DIR / "out" / "pass4_log_enrichment_60d"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FEATURES = OUT_DIR / "job_log_features.csv"

# -----
# REQUIRED: define tokens
# -----
# working_tokens = ["ghp_...", "ghp_...", ...]
# If you load from .env, keep that code and ensure `working_tokens` exists.

# -----
# Tunables
# -----
# Selection policy
INCLUDE_ALL_FAILED_INSTRU_JOBS = True
TOP_SLOWEST_SUCCESS_JOBS = 200          # also enrich some successful jobs (most expensive)
MAX_TOTAL_JOBS_TO_FETCH = 1500          # hard cap to prevent runaway
MIN_JOB_DURATION_S = 0                 # allow 0 to include all

# Log download + parsing caps
CONNECT_TIMEOUT = 10
READ_TIMEOUT    = 90
CHUNK_SIZE      = 64 * 1024
MAX_LOG_BYTES   = 15 * 1024 * 1024      # 15MB cap; stop streaming if exceeded

MAX_RETRIES = 6
MAX_WORKERS_CAP = 8
MAX_INFLIGHT_MULT = 20

FLUSH_EVERY_ROWS = 50
FLUSH_MAX_BUFFER = 1000
RESUME_FROM_OUTPUT = True

# -----
# Import V16 detector components (same patterns + mapping functions)
# -----
# This module should be next to this file (created in Step 1).
from instru_signal_v16 import (  # type: ignore
    TRIGGER_PATTERNS_PRIMARY,
    TRIGGER_PATTERNS_ANYWHERE,
    DEVICE_PATTERNS,
    collect_hits_with_groups,
    map_test_invocations,
    map_execution_envs,
    has_android_runtime_evidence,
    has_gmd_gradle_trigger,
    FLUTTER_IT_LINE,
    FLUTTER_IT_ANDROID_HINT,
    FLUTTER_DEVICE_FLAG_RE,
    FLUTTER_DEVICE_IS_ANDROID,
    LINUX_HEADLESS_HINTS_RE,
)

# -------------------------
# Thread-local session
# -------------------------
_thread_local = threading.local()

def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update({"User-Agent": "rq-instru-pass4"})
        _thread_local.session = s
    return _thread_local.session

# -------------------------
# Robust GitHub GET (API calls)
# -------------------------
def gh_get(url: str, tok: str, params=None, stream: bool = False, allow_redirects: bool = True) -> Optional[requests.Response]:
    last = None
    sess = _get_session()

    for attempt in range(MAX_RETRIES):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"token {tok}",
        }
        try:
            r = sess.get(
                url,
                headers=headers,
                params=params,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                stream=stream,
                allow_redirects=allow_redirects,
            )
            last = r
        except requests.exceptions.RequestException:
            time.sleep(min(30, 2 + attempt * 3))
            continue

        # OK
        if r.status_code in (200, 302):
            return r

        rem = (r.headers.get("X-RateLimit-Remaining") or "").strip()
        reset = (r.headers.get("X-RateLimit-Reset") or "").strip()

        msg = ""
        try:
            if "application/json" in (r.headers.get("content-type") or ""):
                msg = (r.json().get("message") or "")
        except Exception:
            msg = ""

        # PRIMARY rate limit
        if r.status_code == 403 and rem == "0" and reset.isdigit():
            sleep_s = max(1, int(reset) - int(time.time()) + 5)
            try:
                r.close()
            except Exception:
                pass
            time.sleep(min(sleep_s, 600))
            continue

        # SECONDARY / ABUSE
        if r.status_code == 403 and ("secondary rate limit" in msg.lower() or "abuse" in msg.lower()):
            try:
                r.close()
            except Exception:
                pass
            time.sleep(120)
            continue

        # transient server errors
        if r.status_code in (500, 502, 503, 504):
            try:
                r.close()
            except Exception:
                pass
            time.sleep(2 + attempt * 2)
            continue

        return r

    return last

# -------------------------
# Fetch job logs as text (handles 302 redirect, zip-or-text, size cap)
# -------------------------
def fetch_job_log_text(owner_repo: str, job_id: str, tok: str) -> Tuple[str, str, int, int]:
    """
    Returns: (text, status, bytes_read, truncated_flag)
    status:
      OK_TEXT, OK_ZIP, HTTP_xxx, TOO_LARGE, EMPTY, ZIP_ERR, STREAM_ERR_*
    """
    api_url = f"https://api.github.com/repos/{owner_repo}/actions/jobs/{job_id}/logs"

    # IMPORTANT: request redirect URL first (302)
    r = gh_get(api_url, tok, stream=False, allow_redirects=False)
    if r is None:
        return "", "NO_RESPONSE", 0, 0

    try:
        if r.status_code == 302:
            dl = r.headers.get("Location") or r.headers.get("location") or ""
            r.close()
            if not dl:
                return "", "NO_LOCATION", 0, 0

            # Download URL is typically unauthenticated; use same session but no auth header needed.
            sess = _get_session()
            try:
                rr = sess.get(dl, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=True)
            except requests.exceptions.RequestException as e:
                return "", f"STREAM_ERR_{type(e).__name__}", 0, 0

            try:
                if rr.status_code != 200:
                    return "", f"HTTP_{rr.status_code}", 0, 0

                buf = io.BytesIO()
                total = 0
                truncated = 0

                for chunk in rr.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_LOG_BYTES:
                        truncated = 1
                        break
                    buf.write(chunk)

                data = buf.getvalue()
                if not data:
                    return "", "EMPTY", total, truncated

                # ZIP sniff
                is_zip = (len(data) >= 2 and data[:2] == b"PK") or ("zip" in (rr.headers.get("content-type") or "").lower())
                if is_zip:
                    try:
                        z = zipfile.ZipFile(io.BytesIO(data))
                        texts = []
                        for nm in z.namelist():
                            b = z.read(nm)
                            if b:
                                texts.append(b.decode("utf-8", errors="ignore"))
                        return "\n\n".join(texts), "OK_ZIP", total, truncated
                    except Exception:
                        return data.decode("utf-8", errors="ignore"), "ZIP_ERR_TEXT", total, truncated

                return data.decode("utf-8", errors="ignore"), "OK_TEXT", total, truncated
            finally:
                try:
                    rr.close()
                except Exception:
                    pass

        # Some setups may return 200 directly (rare)
        if r.status_code != 200:
            return "", f"HTTP_{r.status_code}", 0, 0

        # If 200 from API directly, stream it (could still be zip/text)
        buf = io.BytesIO()
        total = 0
        truncated = 0
        for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_LOG_BYTES:
                truncated = 1
                break
            buf.write(chunk)

        data = buf.getvalue()
        if not data:
            return "", "EMPTY", total, truncated

        is_zip = (len(data) >= 2 and data[:2] == b"PK") or ("zip" in (r.headers.get("content-type") or "").lower())
        if is_zip:
            try:
                z = zipfile.ZipFile(io.BytesIO(data))
                texts = []
                for nm in z.namelist():
                    b = z.read(nm)
                    if b:
                        texts.append(b.decode("utf-8", errors="ignore"))
                return "\n\n".join(texts), "OK_ZIP", total, truncated
            except Exception:
                return data.decode("utf-8", errors="ignore"), "ZIP_ERR_TEXT", total, truncated

        return data.decode("utf-8", errors="ignore"), "OK_TEXT", total, truncated

    except requests.exceptions.RequestException as e:
        return "", f"STREAM_ERR_{type(e).__name__}", 0, 0
    finally:
        try:
            r.close()
        except Exception:
            pass

# -------------------------
# Analyze log text using V16 patterns (incl. GMD)
# -------------------------
def analyze_log_text_v16(text: str) -> Dict[str, object]:
    if not text:
        return {
            "has_text": 0,
            "text_len": 0,
            "truncated": 0,
            "trigger_labels": "",
            "trigger_groups": "",
            "device_labels": "",
            "device_groups": "",
            "Test_Inv_Style_log": "",
            "Exec_Env_Style_log": "",
            "gmd_log": 0,
            "flutter_it_log": 0,
            "flutter_it_androidish_log": 0,
        }

    low = text.lower()

    # Primary triggers first (matches V16 behavior)
    trig_labels, trig_groups = collect_hits_with_groups(TRIGGER_PATTERNS_PRIMARY, low)
    if not trig_labels and not trig_groups:
        trig_labels, trig_groups = collect_hits_with_groups(TRIGGER_PATTERNS_ANYWHERE, low)

    dev_labels, dev_groups = collect_hits_with_groups(DEVICE_PATTERNS, low)

    # Flutter strict gating (mirror V16)
    flutter_it_hits = False
    flutter_it_android_targeted = False
    flutter_devices: List[str] = []

    for m in FLUTTER_IT_LINE.finditer(text):
        flutter_it_hits = True
        line = m.group(0)
        if FLUTTER_IT_ANDROID_HINT.search(line) or FLUTTER_DEVICE_IS_ANDROID.search(line):
            d = FLUTTER_DEVICE_FLAG_RE.search(line)
            if d:
                plat = d.group("dev").strip(' "\'')
                flutter_devices.append(plat)
                if FLUTTER_DEVICE_IS_ANDROID.search(plat) or "android" in plat.lower() or "emulator" in plat.lower():
                    flutter_it_android_targeted = True
            else:
                # no -d provided but strong android hint
                flutter_it_android_targeted = True

    if (not flutter_devices) and LINUX_HEADLESS_HINTS_RE.search(text):
        flutter_devices.append("linux")

    has_android_env_strict = has_android_runtime_evidence(dev_labels, dev_groups)
    flutter_it_androidish = bool(flutter_it_hits and (flutter_it_android_targeted or has_android_env_strict))

    # Map styles using the same V16 mapping functions
    inv_styles = map_test_invocations(trig_groups, trig_labels, flutter_it_androidish)
    env_styles = map_execution_envs(dev_groups, dev_labels)

    return {
        "has_text": 1,
        "text_len": len(text),
        "trigger_labels": "|".join(trig_labels),
        "trigger_groups": "|".join(trig_groups),
        "device_labels": "|".join(dev_labels),
        "device_groups": "|".join(dev_groups),
        "Test_Inv_Style_log": "|".join(inv_styles),
        "Exec_Env_Style_log": "|".join(env_styles),
        "gmd_log": int(has_gmd_gradle_trigger(trig_labels)),
        "flutter_it_log": int(flutter_it_hits),
        "flutter_it_androidish_log": int(flutter_it_androidish),
    }

# -------------------------
# Identify instrumentation jobs from steps/job names using V16 triggers
# -------------------------
def is_instru_job(job_name: str, step_names: List[str]) -> Tuple[int, str, str]:
    text = (job_name or "") + "\n" + "\n".join(step_names or [])
    low = text.lower()

    trig_labels, trig_groups = collect_hits_with_groups(TRIGGER_PATTERNS_PRIMARY, low)
    if not trig_labels and not trig_groups:
        trig_labels, trig_groups = collect_hits_with_groups(TRIGGER_PATTERNS_ANYWHERE, low)

    # Use V16 mapping: non-empty test invocation styles indicates instrumentation-ish
    inv_styles = map_test_invocations(trig_groups, trig_labels, flutter_it_androidish=False)
    flag = 1 if inv_styles else 0
    return flag, "|".join(inv_styles), "|".join(trig_labels)

# -------------------------
# CSV append with lock + fsync (write during run)
# -------------------------
write_lock = threading.Lock()

def flush_rows(rows: List[Dict[str, object]]):
    if not rows:
        return
    OUT_FEATURES.parent.mkdir(parents=True, exist_ok=True)
    header = (not OUT_FEATURES.exists()) or (OUT_FEATURES.stat().st_size == 0)
    df_out = pd.DataFrame(rows)
    with open(OUT_FEATURES, "a", encoding="utf-8", newline="") as f:
        df_out.to_csv(f, index=False, header=header)
        f.flush()
        os.fsync(f.fileno())

# =========================
# MAIN
# =========================
for p in [IN_RUNS, IN_JOBS, IN_STEPS]:
    if not p.exists():
        raise FileNotFoundError(f"Missing required input: {p}")

if "working_tokens" not in globals():
    raise RuntimeError("working_tokens is not defined. Load your tokens first into a list named working_tokens.")

runs = pd.read_csv(IN_RUNS, dtype=str)
jobs = pd.read_csv(IN_JOBS, dtype=str)
steps = pd.read_csv(IN_STEPS, dtype=str)

# Optional run-level metrics (for prioritization); if missing, continue
run_met = None
if IN_RUN_MET.exists():
    try:
        run_met = pd.read_csv(IN_RUN_MET, dtype=str)
    except Exception:
        run_met = None

# Build step list per job
steps_grp = steps.groupby(["owner_repo", "job_id"], dropna=False)["step_name"].apply(list).reset_index()
jobs = jobs.merge(steps_grp, on=["owner_repo", "job_id"], how="left")
jobs["step_name"] = jobs["step_name"].apply(lambda x: x if isinstance(x, list) else [])

# Identify instrumentation-ish jobs using V16 trigger patterns
flags = jobs.apply(lambda r: is_instru_job(str(r.get("job_name","")), r.get("step_name") or []), axis=1, result_type="expand")
jobs["is_instru_job"] = flags[0].astype(int)
jobs["inv_styles_from_steps"] = flags[1].astype(str)
jobs["trig_labels_from_steps"] = flags[2].astype(str)

# Compute duration if timestamps exist
def to_dt(s: str):
    try:
        return pd.to_datetime(s, utc=True, errors="coerce")
    except Exception:
        return pd.NaT

jobs["started_at_dt"] = jobs["started_at"].map(lambda x: to_dt(str(x)))
jobs["completed_at_dt"] = jobs["completed_at"].map(lambda x: to_dt(str(x)))
jobs["job_duration_s"] = (jobs["completed_at_dt"] - jobs["started_at_dt"]).dt.total_seconds()
jobs["job_duration_s"] = jobs["job_duration_s"].fillna(-1).astype(float)

# Filter instrumentation jobs
cand = jobs[(jobs["is_instru_job"] == 1) & (jobs["job_duration_s"] >= MIN_JOB_DURATION_S)].copy()

# Prioritize: all failed + top slowest successes
cand["conclusion_l"] = cand["conclusion"].fillna("").str.lower()
is_success = cand["conclusion_l"].isin(["success"])
is_failed = ~is_success

selected = []
if INCLUDE_ALL_FAILED_INSTRU_JOBS:
    selected.append(cand[is_failed])

if TOP_SLOWEST_SUCCESS_JOBS > 0:
    selected.append(cand[is_success].sort_values("job_duration_s", ascending=False).head(TOP_SLOWEST_SUCCESS_JOBS))

if selected:
    cand_sel = pd.concat(selected, ignore_index=True).drop_duplicates(subset=["owner_repo", "job_id"])
else:
    cand_sel = cand.copy()

# Hard cap
cand_sel = cand_sel.sort_values(["owner_repo", "job_duration_s"], ascending=[True, False]).head(MAX_TOTAL_JOBS_TO_FETCH)

print(f"Instrumentation-ish jobs found: {len(cand)} | selected for logs: {len(cand_sel)}")

# Resume
done_job_ids = set()
if RESUME_FROM_OUTPUT and OUT_FEATURES.exists():
    try:
        prev = pd.read_csv(OUT_FEATURES, usecols=["job_id"], dtype=str)
        done_job_ids = set(prev["job_id"].astype(str))
        print("Resume: already processed job_ids:", len(done_job_ids))
    except Exception:
        pass

todo = cand_sel[~cand_sel["job_id"].astype(str).isin(done_job_ids)].copy()
row_list = todo.to_dict(orient="records")
total_rows = len(row_list)
if total_rows == 0:
    print("Nothing to do (all selected jobs already processed).")
    raise SystemExit(0)

# Tokens/workers
worker_tokens = [t for t in working_tokens if str(t).strip()]
if not worker_tokens:
    raise RuntimeError("No working tokens available.")

max_workers = min(MAX_WORKERS_CAP, len(worker_tokens))
max_inflight = max_workers * MAX_INFLIGHT_MULT

rows_buf: List[Dict[str, object]] = []
completed = 0
next_idx = 0

def process_one(row: Dict[str, object], tok: str) -> Dict[str, object]:
    owner_repo = str(row.get("owner_repo","")).strip()
    run_id     = str(row.get("run_id","")).strip()
    job_id     = str(row.get("job_id","")).strip()
    job_name   = str(row.get("job_name","")).strip()
    concl      = str(row.get("conclusion","")).strip()
    st_at      = str(row.get("started_at","")).strip()
    cp_at      = str(row.get("completed_at","")).strip()
    dur_s      = float(row.get("job_duration_s", -1))

    text, st, nbytes, truncated = fetch_job_log_text(owner_repo, job_id, tok)
    det = analyze_log_text_v16(text)
    det["truncated"] = int(truncated or det.get("truncated", 0))

    return {
        "owner_repo": owner_repo,
        "run_id": run_id,
        "job_id": job_id,
        "job_name": job_name,
        "job_conclusion": concl,
        "started_at": st_at,
        "completed_at": cp_at,
        "job_duration_s": dur_s,
        "inv_styles_from_steps": str(row.get("inv_styles_from_steps","")),
        "trig_labels_from_steps": str(row.get("trig_labels_from_steps","")),
        "logs_status": st,
        "log_bytes_read": int(nbytes),
        **det,
    }

with ThreadPoolExecutor(max_workers=max_workers) as ex:
    futures = set()

    # prime
    while next_idx < total_rows and len(futures) < max_inflight:
        row = row_list[next_idx]
        tok = worker_tokens[next_idx % len(worker_tokens)]
        futures.add(ex.submit(process_one, row, tok))
        next_idx += 1

    while futures:
        for fut in as_completed(futures):
            futures.remove(fut)

            try:
                out = fut.result()
            except Exception as e:
                out = {"logs_status": "EXCEPTION", "error": repr(e)}

            rows_buf.append(out)
            completed += 1

            # refill
            if next_idx < total_rows:
                row = row_list[next_idx]
                tok = worker_tokens[next_idx % len(worker_tokens)]
                futures.add(ex.submit(process_one, row, tok))
                next_idx += 1

            # flush
            if (completed % FLUSH_EVERY_ROWS) == 0 or len(rows_buf) >= FLUSH_MAX_BUFFER:
                with write_lock:
                    flush_rows(rows_buf)
                rows_buf = []
                print(f"Completed {completed}/{total_rows} | saved -> {OUT_FEATURES}")

            break

# final flush
if rows_buf:
    with write_lock:
        flush_rows(rows_buf)

print("Done. Wrote:", OUT_FEATURES)
