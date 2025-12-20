# run_all_passes_to_part_b2.py
import os
import re
import sys
import csv
import shutil
import runpy
import subprocess
from pathlib import Path
import io
import contextlib
from datetime import datetime

# ============================================================
# USER CONFIG
# ============================================================

TOKENS_ENV_FILE = Path(r"C:\GitHub\Android-Mobile-Apps\All_Tokens.env")
TOKEN_KEYS_123  = ["GITHUB_TOKEN_1", "GITHUB_TOKEN_2", "GITHUB_TOKEN_3"]

PIPELINE_SRC = Path(r"C:\GitHub\Android-Mobile-Apps\ICST2026\7 - Searching Pipeline - Instru test Performance")

RESULT_ROOT = Path(
    r"C:\Android Mobile App\Step3_Instr_Testing_Analysis\Type_1\Aug_10\ICST2026\3 - RQ3\Part_B2"
)

# Runtime folder (all outputs go here)
RUNTIME_DIR = RESULT_ROOT / "search_pipeline_runtime"

# Logs folder
LOG_DIR = RUNTIME_DIR / "logs"

# Your REAL clones root (contains owner__repo folders)
CLONES_ROOT_REAL = Path(
    r"C:\Android Mobile App\Step3_Instr_Testing_Analysis\Type_1\Aug_10\ICST2026\2 - RQ2\Clone_Full"
)

# Repo list CSV
URL_LIST = RESULT_ROOT / "URL_List.csv"

# ============================================================
# Helpers
# ============================================================

def require_file(path: Path, hint: str):
    if not path.exists():
        raise RuntimeError(
            f"\nRequired file was not created:\n  {path}\n\n"
            f"{hint}\n"
            f"Check logs in:\n  {LOG_DIR}\n"
        )

def ensure_exists(path: Path, what: str):
    if not path.exists():
        raise FileNotFoundError(f"{what} not found: {path}")

def load_env_file(path: Path) -> None:
    """
    Loads KEY=VALUE lines from a .env-like file into os.environ (non-destructive).
    Supports:
      GITHUB_TOKEN_1=...
      export GITHUB_TOKEN_2="..."
      # comments
    """
    if not path.exists():
        raise FileNotFoundError(f"Missing env file: {path}")

    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")

        if k and v and k not in os.environ:
            os.environ[k] = v

def load_tokens_123() -> list[str]:
    try:
        load_env_file(TOKENS_ENV_FILE)
    except Exception as e:
        print(f"[warn] Could not load tokens env file: {e}")

    toks: list[str] = []
    for k in TOKEN_KEYS_123:
        v = os.environ.get(k, "").strip()
        if v:
            toks.append(v)

    if not toks:
        raise RuntimeError(
            "No tokens found. Please set at least one of:\n"
            "  GITHUB_TOKEN_1, GITHUB_TOKEN_2, GITHUB_TOKEN_3\n"
            f"Checked .env file: {TOKENS_ENV_FILE}\n"
        )
    return toks

def copy_pipeline_files():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    required = [
        "instru_signal_v16.py",
        "pass1_build_workflow_catalog.py",
        "pass2_fetch_recent_runs_jobs_steps.py",
        "pass3_compute_instru_performance_metrics.py",
        "pass4_fetch_job_logs_enrich_v16.py",
    ]

    for fname in required:
        src = PIPELINE_SRC / fname
        ensure_exists(src, f"Pipeline file '{fname}'")
        shutil.copy2(src, RUNTIME_DIR / fname)

def make_clones_mirror() -> Path:
    """
    Creates a mirror directory structure:
      CLONES_MIRROR/owner/repo  -> junction to CLONES_ROOT_REAL/owner__repo

    This makes pass1 work even if it expects owner/repo layout.
    """
    mirror = RUNTIME_DIR / "clones_mirror"
    mirror.mkdir(parents=True, exist_ok=True)

    made = 0
    skipped = 0
    failed = 0

    for src in CLONES_ROOT_REAL.iterdir():
        if not src.is_dir():
            continue

        name = src.name
        if "__" not in name:
            continue

        owner, repo = name.split("__", 1)
        if not owner or not repo:
            continue

        dest = mirror / owner / repo
        if dest.exists():
            skipped += 1
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)

        # Create a directory junction (does not require admin, unlike symlink usually)
        try:
            cp = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(dest), str(src)],
                check=True, capture_output=True, text=True
            )
            made += 1
        except Exception:
            failed += 1

    print(f"[mirror] clones_mirror created at: {mirror}")
    print(f"[mirror] junctions made={made}, skipped={skipped}, failed={failed}")
    return mirror

def patch_pass1_paths(clones_root_for_pass1: Path):
    """
    Patch Pass 1 to your real local paths, and point CLONES_ROOT to clones_mirror.
    Uses lambda replacements so Windows backslashes don't break re.sub.
    """
    pass1 = RUNTIME_DIR / "pass1_build_workflow_catalog.py"
    txt = pass1.read_text(encoding="utf-8", errors="ignore")

    out_catalog = RUNTIME_DIR / "out" / "workflow_catalog_v16.csv"
    out_catalog.parent.mkdir(parents=True, exist_ok=True)

    txt2 = re.sub(
        r'^URL_LIST\s*=\s*Path\(r".*?"\)\s*$',
        lambda _m: f'URL_LIST = Path(r"{URL_LIST}")',
        txt,
        flags=re.MULTILINE
    )

    txt2 = re.sub(
        r'^CLONES_ROOT\s*=\s*Path\(r".*?"\)\s*$',
        lambda _m: f'CLONES_ROOT = Path(r"{clones_root_for_pass1}")',
        txt2,
        flags=re.MULTILINE
    )

    txt2 = re.sub(
        r'^OUT\s*=\s*Path\(r".*?"\)\s*$',
        lambda _m: f'OUT = Path(r"{out_catalog}")',
        txt2,
        flags=re.MULTILINE
    )

    pass1.write_text(txt2, encoding="utf-8")
    print("[patch] pass1 paths set (CLONES_ROOT -> clones_mirror)")

def patch_pass3_runtime_file():
    """
    Fix pass3 in the runtime copy so it runs on Python 3.10:
      - remove `from __future__ import annotations`
      - ensure `import re` exists if `re.` is used
    """
    p3 = RUNTIME_DIR / "pass3_compute_instru_performance_metrics.py"
    if not p3.exists():
        return

    txt = p3.read_text(encoding="utf-8", errors="ignore")

    txt = re.sub(
        r'^\s*from\s+__future__\s+import\s+annotations\s*\r?\n',
        '',
        txt,
        flags=re.MULTILINE
    )

    uses_re = "re." in txt
    has_re_import = bool(re.search(r'^\s*import\s+re\s*$', txt, flags=re.MULTILINE)) or \
                    bool(re.search(r'^\s*from\s+re\s+import\s+', txt, flags=re.MULTILINE))

    if uses_re and not has_re_import:
        lines = txt.splitlines(True)
        i = 0
        if i < len(lines) and lines[i].startswith("#!"):
            i += 1
        if i < len(lines) and re.search(r'coding[:=]\s*[-\w.]+', lines[i]):
            i += 1
        if i < len(lines) and re.match(r'^\s*(\"\"\"|\'\'\')', lines[i]):
            quote = '"""' if '"""' in lines[i] else "'''"
            if lines[i].count(quote) >= 2:
                i += 1
            else:
                i += 1
                while i < len(lines) and quote not in lines[i]:
                    i += 1
                if i < len(lines):
                    i += 1
        lines.insert(i, "import re\n")
        txt = "".join(lines)

    p3.write_text(txt, encoding="utf-8")
    print("[patch] pass3 fixed (future import removed, re ensured)")

def run_script(path: Path, init_globals: dict | None = None):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{path.stem}_{stamp}.log"

    print(f"\n=== RUN: {path.name} ===")
    print(f"-> Logging to: {log_path}")

    sys.path.insert(0, str(RUNTIME_DIR))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            runpy.run_path(str(path), run_name="__main__", init_globals=init_globals or {})
    except Exception:
        log_path.write_text(buf.getvalue(), encoding="utf-8", errors="ignore")
        raise
    finally:
        while sys.path and sys.path[0] == str(RUNTIME_DIR):
            sys.path.pop(0)

    log_path.write_text(buf.getvalue(), encoding="utf-8", errors="ignore")
    print(f"✓ Completed {path.name}")

# ============================================================
# Main
# ============================================================

def main():
    ensure_exists(PIPELINE_SRC, "PIPELINE_SRC folder")
    ensure_exists(RESULT_ROOT, "RESULT_ROOT folder")
    ensure_exists(URL_LIST, "URL_LIST CSV")
    ensure_exists(CLONES_ROOT_REAL, "CLONES_ROOT_REAL folder")

    tokens = load_tokens_123()
    print(f"Using tokens (only 1..3): {len(tokens)}")
    print(f"Pipeline source: {PIPELINE_SRC}")
    print(f"Runtime/output root: {RUNTIME_DIR}")

    copy_pipeline_files()

    # Build mirror so pass1 can find clones as owner/repo
    clones_mirror = make_clones_mirror()

    # Patch pass1 to use mirror + correct URL_LIST/OUT
    patch_pass1_paths(clones_mirror)

    # Patch pass3 python3.10 safety
    patch_pass3_runtime_file()

    # Pass 1
    run_script(RUNTIME_DIR / "pass1_build_workflow_catalog.py")

    # Pass 2
    inject = {"working_tokens": tokens}
    run_script(RUNTIME_DIR / "pass2_fetch_recent_runs_jobs_steps.py", init_globals=inject)

    # Require pass2 output before pass3
    pass2_runs = RUNTIME_DIR / "out" / "pass2_recent_60d" / "runs_recent.csv"
    require_file(
        pass2_runs,
        hint="Pass2 produced no runs_recent.csv. If clones_mirror made few/zero junctions, "
             "your clone folders might not be owner__repo OR CLONES_ROOT_REAL is wrong. "
             "Check [mirror] counts and the pass1 log."
    )

    # Pass 3–4
    run_script(RUNTIME_DIR / "pass3_compute_instru_performance_metrics.py", init_globals=inject)
    run_script(RUNTIME_DIR / "pass4_fetch_job_logs_enrich_v16.py", init_globals=inject)

    print("\nDone.")
    print(f"All outputs are under: {RUNTIME_DIR / 'out'}")
    print(f"Logs are under: {LOG_DIR}")

if __name__ == "__main__":
    main()
