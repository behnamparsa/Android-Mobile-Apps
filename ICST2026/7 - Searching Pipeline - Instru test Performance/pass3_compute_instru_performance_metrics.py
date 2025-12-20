# =========================
# PASS 3: Compute instrumentation-testing performance metrics (last 60 days)
#
# Goal:
#   Using:
#     - Pass 1 catalog (instru_signal_v16 outputs per workflow file)
#     - Pass 2 runs/jobs/steps timestamps
#   produce run-level + workflow-level performance metrics for instrumentation tests.
#
# Design notes:
# - Performance is computed from jobs/steps started_at/completed_at timestamps (no logs required).
# - We preserve V16 detection fields by joining workflows via workflow.path from GitHub API.
# - If GitHub API mapping cannot be fetched, we still compute metrics by workflow_id alone (styles blank).
#
# INPUT:
#   out/workflow_catalog_v16.csv
#   out/pass2_recent_60d/runs_recent.csv
#   out/pass2_recent_60d/jobs_recent.csv
#   out/pass2_recent_60d/steps_recent.csv
#
# OUTPUT:
#   out/pass3_metrics_60d/workflow_map.csv
#   out/pass3_metrics_60d/run_metrics.csv
#   out/pass3_metrics_60d/workflow_metrics.csv
#   out/pass3_metrics_60d/repo_metrics.csv
# =========================

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests


# -------------------------
# PATHS
# -------------------------
BASE_DIR = Path(__file__).resolve().parent

IN_CATALOG = BASE_DIR / "out" / "workflow_catalog_v16.csv"

P2_DIR = BASE_DIR / "out" / "pass2_recent_60d"
IN_RUNS  = P2_DIR / "runs_recent.csv"
IN_JOBS  = P2_DIR / "jobs_recent.csv"
IN_STEPS = P2_DIR / "steps_recent.csv"

OUT_DIR = BASE_DIR / "out" / "pass3_metrics_60d"
OUT_DIR.mkdir(parents=True, exist_ok=True)

OUT_WF_MAP     = OUT_DIR / "workflow_map.csv"
OUT_RUN_MET    = OUT_DIR / "run_metrics.csv"
OUT_WF_MET     = OUT_DIR / "workflow_metrics.csv"
OUT_REPO_MET   = OUT_DIR / "repo_metrics.csv"


# -------------------------
# REQUIRED: define tokens
# -------------------------
# working_tokens = ["ghp_...", "ghp_...", ...]
if "working_tokens" not in globals():
    raise RuntimeError("working_tokens is not defined. Load your tokens first into a list named working_tokens.")

TOKENS = [t for t in working_tokens if str(t).strip()]
if not TOKENS:
    raise RuntimeError("No working tokens available.")


# -------------------------
# Tunables
# -------------------------
CONNECT_TIMEOUT = 10
READ_TIMEOUT    = 60
MAX_RETRIES     = 6

MAX_WORKERS_CAP = 8

# -------------------------
# Thread-local session
# -------------------------
_thread_local = threading.local()

def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update({"User-Agent": "rq-instru-pass3"})
        _thread_local.session = s
    return _thread_local.session


def gh_get(url: str, tok: str, params=None, max_retries: int = MAX_RETRIES) -> Optional[requests.Response]:
    """Robust GitHub GET with token rotation support."""
    last = None
    sess = _get_session()

    for attempt in range(max_retries):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"token {tok}",
        }
        try:
            r = sess.get(url, headers=headers, params=params, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            last = r
        except requests.exceptions.RequestException:
            time.sleep(min(30, 2 + attempt * 3))
            continue

        if r.status_code == 200:
            return r

        # Rate limit handling
        rem = (r.headers.get("X-RateLimit-Remaining") or "").strip()
        reset = (r.headers.get("X-RateLimit-Reset") or "").strip()

        msg = ""
        try:
            if "application/json" in (r.headers.get("content-type") or ""):
                msg = (r.json().get("message") or "")
        except Exception:
            msg = ""

        if r.status_code == 403 and rem == "0" and reset.isdigit():
            sleep_s = max(1, int(reset) - int(time.time()) + 5)
            try:
                r.close()
            except Exception:
                pass
            time.sleep(min(sleep_s, 600))
            continue

        if r.status_code == 403 and ("secondary rate limit" in msg.lower() or "abuse" in msg.lower()):
            try:
                r.close()
            except Exception:
                pass
            time.sleep(120)
            continue

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
# Matching + time parsing
# -------------------------
def norm_workflow_path(p: str) -> str:
    if not p:
        return ""
    p = str(p).replace("\\", "/").strip()
    if p.startswith("./"):
        p = p[2:]
    if not p.startswith(".github/workflows/") and ("/.github/workflows/" in p):
        # full path -> rel from repo root
        ix = p.find("/.github/workflows/")
        p = p[ix+1:]
    return p


def to_dt(s: Any):
    """Parse GitHub ISO timestamps safely."""
    if not s or not isinstance(s, str):
        return pd.NaT
    try:
        return pd.to_datetime(s, utc=True, errors="coerce")
    except Exception:
        return pd.NaT


def dur_seconds(start, end) -> float:
    if pd.isna(start) or pd.isna(end):
        return float("nan")
    try:
        return (end - start).total_seconds()
    except Exception:
        return float("nan")


# -------------------------
# Step classification (lightweight)
# -------------------------
RE_TEST_EXEC = re.compile(
    r"(connected(android)?test|connectedcheck|devicecheck|androidtest|instrumentation|espresso|ui\s*test|e2e\b|marathon|spoon|flank\b|firebase\s+test\s+android|manageddevice|managed\s*device|gmd\b)",
    re.IGNORECASE,
)
RE_SETUP = re.compile(
    r"(sdkmanager|avdmanager|create\s+avd|emulator|start\s+emulator|boot|system-images;android-|install\s+android\s+sdk|setup\s+android|gradle\s+cache|java\s+setup|jdk|android\s+emulator\s+runner)",
    re.IGNORECASE,
)
RE_BUILD = re.compile(
    r"(assemble|build|compile|lint|ktlint|detekt|unit\s*test|testDebugUnitTest)",
    re.IGNORECASE,
)


def classify_step(step_name: str) -> str:
    n = (step_name or "").strip()
    if not n:
        return "unknown"
    if RE_TEST_EXEC.search(n):
        return "test_exec"
    if RE_SETUP.search(n):
        return "setup"
    if RE_BUILD.search(n):
        return "build"
    return "other"


# -------------------------
# Fetch workflow map: workflow_id -> workflow_path via API
# -------------------------
def list_workflows(owner_repo: str, tok: str) -> Tuple[List[Dict[str, Any]], str]:
    url = f"https://api.github.com/repos/{owner_repo}/actions/workflows"
    r = gh_get(url, tok, params={"per_page": 100})
    if r is None:
        return [], "NO_RESPONSE"
    try:
        if r.status_code != 200:
            return [], f"HTTP_{r.status_code}"
        js = r.json() or {}
        wfs = js.get("workflows") or []
        return wfs, "OK"
    except Exception:
        return [], "JSON_ERR"
    finally:
        try:
            r.close()
        except Exception:
            pass


def build_workflow_map(owner_repos: List[str]) -> pd.DataFrame:
    """Return mapping rows: owner_repo, workflow_id, workflow_path, workflow_name, state."""
    rows = []
    max_workers = min(MAX_WORKERS_CAP, max(1, len(TOKENS)))

    lock = threading.Lock()
    idx = {"i": 0}

    def next_tok() -> str:
        with lock:
            t = TOKENS[idx["i"] % len(TOKENS)]
            idx["i"] += 1
            return t

    def worker(owner_repo: str) -> Dict[str, Any]:
        tok = next_tok()
        wfs, st = list_workflows(owner_repo, tok)
        out = []
        for wf in wfs:
            out.append({
                "owner_repo": owner_repo,
                "workflow_id": wf.get("id"),
                "workflow_name_api": wf.get("name") or "",
                "workflow_path_api": norm_workflow_path(wf.get("path") or ""),
                "state_api": wf.get("state") or "",
                "wf_status": st,
            })
        if not out:
            out.append({
                "owner_repo": owner_repo,
                "workflow_id": "",
                "workflow_name_api": "",
                "workflow_path_api": "",
                "state_api": "",
                "wf_status": st,
            })
        return {"rows": out}

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(worker, r): r for r in owner_repos}
        for fut in as_completed(futs):
            try:
                res = fut.result()
                rows.extend(res.get("rows") or [])
            except Exception as e:
                rows.append({
                    "owner_repo": futs[fut],
                    "workflow_id": "",
                    "workflow_name_api": "",
                    "workflow_path_api": "",
                    "state_api": "",
                    "wf_status": "EXCEPTION",
                    "error": repr(e),
                })

    return pd.DataFrame(rows)


# -------------------------
# MAIN
# -------------------------
for p in [IN_CATALOG, IN_RUNS, IN_JOBS, IN_STEPS]:
    if not p.exists():
        raise FileNotFoundError(f"Missing required input: {p}")

cat = pd.read_csv(IN_CATALOG, dtype=str)
runs = pd.read_csv(IN_RUNS, dtype=str)
jobs = pd.read_csv(IN_JOBS, dtype=str)
steps = pd.read_csv(IN_STEPS, dtype=str)

# normalize + restrict to workflows where V16 detected instrumentation
if "workflow_path" in cat.columns:
    cat["wf_path_norm"] = cat["workflow_path"].map(norm_workflow_path)
else:
    # Pass 1 always writes workflow_path, but keep safety
    cat["wf_path_norm"] = ""

sig_cols = []
for c in ["instru_t_ci_signal", "called_instru_t_ci_signal"]:
    if c in cat.columns:
        sig_cols.append(c)

if not sig_cols:
    raise RuntimeError("Catalog missing instru signal columns (instru_t_ci_signal / called_instru_t_ci_signal).")

def truthy(v: Any) -> bool:
    s = str(v).strip().lower()
    return s in ("1", "true", "t", "yes", "y")

cat["is_instru"] = False
for c in sig_cols:
    cat["is_instru"] = cat["is_instru"] | cat[c].map(truthy)

cat_instru = cat[cat["is_instru"]].copy()

# Build workflow map via API for repos present in Pass2 outputs
owner_repos = sorted(set(runs["owner_repo"].dropna().astype(str)))
print(f"Pass3: building workflow_id->path map for repos: {len(owner_repos)}")
wf_map_api = build_workflow_map(owner_repos)

# Join API map to catalog by owner_repo + workflow_path
wf_map_api["workflow_id"] = wf_map_api["workflow_id"].astype(str)

cat_instru["wf_path_norm"] = cat_instru["wf_path_norm"].astype(str)
wf_map = wf_map_api.merge(
    cat_instru,
    how="left",
    left_on=["owner_repo", "workflow_path_api"],
    right_on=["full_name", "wf_path_norm"],
    suffixes=("", "_cat"),
)

# Reduce to mapping we need
keep_cols = [
    "owner_repo", "workflow_id", "workflow_name_api", "workflow_path_api", "state_api", "wf_status",
    # V16 fields (if present)
    "instru_t_ci_signal", "called_instru_t_ci_signal",
    "Exec_Env_Style", "Test_Inv_Style", "execution_environment", "test_invocation",
    "flutter_integ_t_signal", "third_party_env_label",
    "followed_files_count", "unresolved_dynamic_refs_count",
]
for c in keep_cols:
    if c not in wf_map.columns:
        wf_map[c] = ""
wf_map_out = wf_map[keep_cols].copy()
wf_map_out.to_csv(OUT_WF_MAP, index=False, encoding="utf-8-sig")
print(f"Wrote workflow map: {OUT_WF_MAP} rows={len(wf_map_out)}")

# Merge styles into runs/jobs/steps by owner_repo+workflow_id
def attach_styles(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["workflow_id"] = out["workflow_id"].astype(str)
    merged = out.merge(
        wf_map_out,
        how="left",
        on=["owner_repo", "workflow_id"],
        suffixes=("", "_map"),
    )
    return merged

runs_m = attach_styles(runs)
jobs_m = attach_styles(jobs)
steps_m = attach_styles(steps)

# Parse timestamps + durations
jobs_m["started_dt"] = jobs_m["started_at"].map(to_dt)
jobs_m["completed_dt"] = jobs_m["completed_at"].map(to_dt)
jobs_m["job_dur_s"] = [dur_seconds(s, e) for s, e in zip(jobs_m["started_dt"], jobs_m["completed_dt"])]

steps_m["started_dt"] = steps_m["started_at"].map(to_dt)
steps_m["completed_dt"] = steps_m["completed_at"].map(to_dt)
steps_m["step_dur_s"] = [dur_seconds(s, e) for s, e in zip(steps_m["started_dt"], steps_m["completed_dt"])]

steps_m["step_bucket"] = steps_m["step_name"].map(classify_step)

# Identify instrumentation jobs: any job that has a test_exec step
job_has_test = (
    steps_m[steps_m["step_bucket"] == "test_exec"]
    .groupby(["owner_repo", "workflow_id", "run_id", "run_attempt", "job_id"], dropna=False)
    .size()
    .reset_index(name="test_exec_step_count")
)
job_has_test["is_instru_job"] = 1

jobs_m["job_id"] = jobs_m["job_id"].astype(str)
job_has_test["job_id"] = job_has_test["job_id"].astype(str)
jobs_m = jobs_m.merge(
    job_has_test[["owner_repo", "workflow_id", "run_id", "run_attempt", "job_id", "is_instru_job"]],
    how="left",
    on=["owner_repo", "workflow_id", "run_id", "run_attempt", "job_id"],
)
jobs_m["is_instru_job"] = jobs_m["is_instru_job"].fillna(0).astype(int)

# Build run-level aggregates
# Step bucket sums (only within instru jobs)
steps_instru = steps_m.merge(
    jobs_m[["owner_repo", "workflow_id", "run_id", "run_attempt", "job_id", "is_instru_job"]],
    how="left",
    on=["owner_repo", "workflow_id", "run_id", "run_attempt", "job_id"],
)
steps_instru = steps_instru[steps_instru["is_instru_job"] == 1].copy()

bucket_sums = (
    steps_instru.groupby(["owner_repo", "workflow_id", "run_id", "run_attempt", "step_bucket"], dropna=False)["step_dur_s"]
    .sum(min_count=1)
    .reset_index()
)
bucket_pivot = bucket_sums.pivot_table(
    index=["owner_repo", "workflow_id", "run_id", "run_attempt"],
    columns="step_bucket",
    values="step_dur_s",
    aggfunc="first",
).reset_index()

for col in ["test_exec", "setup", "build", "other", "unknown"]:
    if col not in bucket_pivot.columns:
        bucket_pivot[col] = float("nan")

# Instru job duration sum
instru_job_sums = (
    jobs_m[jobs_m["is_instru_job"] == 1]
    .groupby(["owner_repo", "workflow_id", "run_id", "run_attempt"], dropna=False)["job_dur_s"]
    .sum(min_count=1)
    .reset_index(name="instru_jobs_total_dur_s")
)

instru_job_counts = (
    jobs_m[jobs_m["is_instru_job"] == 1]
    .groupby(["owner_repo", "workflow_id", "run_id", "run_attempt"], dropna=False)
    .size().reset_index(name="instru_job_count")
)

# Merge into runs
runs_m["workflow_id"] = runs_m["workflow_id"].astype(str)
runs_m["run_id"] = runs_m["run_id"].astype(str)
runs_m["run_attempt"] = runs_m["run_attempt"].astype(str)

bucket_pivot["workflow_id"] = bucket_pivot["workflow_id"].astype(str)
bucket_pivot["run_id"] = bucket_pivot["run_id"].astype(str)
bucket_pivot["run_attempt"] = bucket_pivot["run_attempt"].astype(str)

instru_job_sums["workflow_id"] = instru_job_sums["workflow_id"].astype(str)
instru_job_sums["run_id"] = instru_job_sums["run_id"].astype(str)
instru_job_sums["run_attempt"] = instru_job_sums["run_attempt"].astype(str)

instru_job_counts["workflow_id"] = instru_job_counts["workflow_id"].astype(str)
instru_job_counts["run_id"] = instru_job_counts["run_id"].astype(str)
instru_job_counts["run_attempt"] = instru_job_counts["run_attempt"].astype(str)

run_met = runs_m.merge(bucket_pivot, how="left", on=["owner_repo", "workflow_id", "run_id", "run_attempt"])
run_met = run_met.merge(instru_job_sums, how="left", on=["owner_repo", "workflow_id", "run_id", "run_attempt"])
run_met = run_met.merge(instru_job_counts, how="left", on=["owner_repo", "workflow_id", "run_id", "run_attempt"])

# A simple "has instrumentation execution in run" flag
run_met["has_instru_exec"] = (~run_met["instru_job_count"].isna()) & (run_met["instru_job_count"] > 0)
run_met["has_instru_exec"] = run_met["has_instru_exec"].astype(int)

# Rename bucket columns into clearer metric names
run_met = run_met.rename(columns={
    "test_exec": "test_exec_dur_s",
    "setup": "setup_dur_s",
    "build": "build_dur_s",
    "other": "other_dur_s",
    "unknown": "unknown_dur_s",
})

run_met.to_csv(OUT_RUN_MET, index=False, encoding="utf-8-sig")
print(f"Wrote run metrics: {OUT_RUN_MET} rows={len(run_met)}")

# Workflow-level aggregates (only runs with instrumentation execution)
runs_exec = run_met[run_met["has_instru_exec"] == 1].copy()

def q95(x):
    try:
        return x.quantile(0.95)
    except Exception:
        return float("nan")

wf_grp = runs_exec.groupby(["owner_repo", "workflow_id"], dropna=False)

wf_met = wf_grp.agg(
    runs=("run_id", "nunique"),
    success_rate=("conclusion", lambda s: (s.astype(str).str.lower() == "success").mean()),
    failure_rate=("conclusion", lambda s: (s.astype(str).str.lower() == "failure").mean()),
    cancelled_rate=("conclusion", lambda s: (s.astype(str).str.lower() == "cancelled").mean()),
    median_test_exec_s=("test_exec_dur_s", "median"),
    p95_test_exec_s=("test_exec_dur_s", q95),
    median_instru_jobs_total_s=("instru_jobs_total_dur_s", "median"),
    p95_instru_jobs_total_s=("instru_jobs_total_dur_s", q95),
    median_setup_s=("setup_dur_s", "median"),
    p95_setup_s=("setup_dur_s", q95),
).reset_index()

# Attach styles
wf_met = wf_met.merge(
    wf_map_out.drop_duplicates(subset=["owner_repo", "workflow_id"]),
    how="left",
    on=["owner_repo", "workflow_id"],
)
wf_met.to_csv(OUT_WF_MET, index=False, encoding="utf-8-sig")
print(f"Wrote workflow metrics: {OUT_WF_MET} rows={len(wf_met)}")

# Repo-level aggregates
repo_grp = runs_exec.groupby(["owner_repo"], dropna=False)
repo_met = repo_grp.agg(
    runs=("run_id", "nunique"),
    workflows=("workflow_id", "nunique"),
    success_rate=("conclusion", lambda s: (s.astype(str).str.lower() == "success").mean()),
    failure_rate=("conclusion", lambda s: (s.astype(str).str.lower() == "failure").mean()),
    median_test_exec_s=("test_exec_dur_s", "median"),
    p95_test_exec_s=("test_exec_dur_s", q95),
    median_instru_jobs_total_s=("instru_jobs_total_dur_s", "median"),
    p95_instru_jobs_total_s=("instru_jobs_total_dur_s", q95),
).reset_index()

repo_met.to_csv(OUT_REPO_MET, index=False, encoding="utf-8-sig")
print(f"Wrote repo metrics: {OUT_REPO_MET} rows={len(repo_met)}")

print("PASS 3 complete.")
