from __future__ import annotations

from pathlib import Path
import pandas as pd
import re

from instru_signal_v16 import analyze_ci_yaml_file  # your refactored function

# ============================================================
# CONFIG (EDIT THESE)
# ============================================================

URL_LIST = Path(r"C:\Android Mobile App\Step3_Instr_Testing_Analysis\Type_1\Aug_10\ICST2026\3 - RQ3\Part_B2\URL_List.csv")

# IMPORTANT: this folder contains clones like: ge0rg__aprsdroid
CLONES_ROOT = Path(r"C:\Android Mobile App\Step3_Instr_Testing_Analysis\Type_1\Aug_10\ICST2026\2 - RQ2\Clone_Full")

OUT = Path(r"C:\Android Mobile App\Step3_Instr_Testing_Analysis\Type_1\Aug_10\ICST2026\3 - RQ3\Part_B2\search_pipeline_runtime\out\workflow_catalog_v16.csv")
OUT.parent.mkdir(parents=True, exist_ok=True)

# Which CSV column has repo URLs?
# Your earlier scripts used repo_url; this pass1 draft uses html_url.
# We'll auto-detect.
POSSIBLE_URL_COLUMNS = ["repo_url", "html_url", "url"]

# ============================================================
# Helpers
# ============================================================

GITHUB_URL_RE = re.compile(
    r"""(?xi)
    github\.com/
    (?P<owner>[^/\s]+)
    /
    (?P<repo>[^/\s]+?)
    (?:\.git)?            # optional .git
    (?:/.*)?$             # allow extra path segments
    """
)

def parse_owner_repo(url: str) -> str:
    """
    Returns "owner/repo" from a GitHub URL.
    Handles:
      - https://github.com/owner/repo
      - http(s)://github.com/owner/repo.git
      - github.com/owner/repo/...
    """
    if url is None:
        return ""
    u = str(url).strip()
    if not u:
        return ""
    u = u.rstrip("/")

    m = GITHUB_URL_RE.search(u)
    if not m:
        return ""
    owner = m.group("owner").strip()
    repo = m.group("repo").strip().removesuffix(".git")
    return f"{owner}/{repo}" if owner and repo else ""

def guess_repo_root(owner_repo: str) -> Path | None:
    """
    Finds the local clone folder for a repo.
    Supports your convention:
      CLONES_ROOT/owner__repo
    Also tries a few other common layouts.
    """
    if not owner_repo or "/" not in owner_repo:
        return None

    owner, repo = owner_repo.split("/", 1)
    repo = repo.removesuffix(".git")

    candidates = [
        # Your convention
        CLONES_ROOT / f"{owner}__{repo}",

        # Common alternatives (just in case)
        CLONES_ROOT / owner / repo,
        CLONES_ROOT / repo,
        CLONES_ROOT / owner_repo.replace("/", "__"),
        CLONES_ROOT / owner_repo,
    ]

    for c in candidates:
        if c.exists() and ((c / ".git").exists() or (c / ".github").exists()):
            return c

    # Last resort: if folder exists but markers not present
    for c in candidates:
        if c.exists():
            return c

    return None

def find_url_column(df: pd.DataFrame) -> str:
    cols = [c for c in df.columns]
    for c in POSSIBLE_URL_COLUMNS:
        if c in cols:
            return c
    raise KeyError(
        f"URL column not found. Expected one of {POSSIBLE_URL_COLUMNS}. "
        f"Found columns: {cols}"
    )

# ============================================================
# Main
# ============================================================

assert URL_LIST.exists(), f"URL_LIST not found: {URL_LIST}"
assert CLONES_ROOT.exists(), f"CLONES_ROOT not found: {CLONES_ROOT}"

df = pd.read_csv(URL_LIST, dtype=str)
url_col = find_url_column(df)

rows: list[dict] = []

n_total_urls = 0
n_parsed = 0
n_clone_found = 0
n_clone_missing = 0
n_no_workflows_dir = 0
n_workflow_files = 0

for url in df[url_col].dropna().tolist():
    n_total_urls += 1
    owner_repo = parse_owner_repo(url)
    if not owner_repo:
        continue
    n_parsed += 1

    repo_root = guess_repo_root(owner_repo)
    if repo_root is None:
        n_clone_missing += 1
        rows.append({
            "full_name": owner_repo,
            "repo_root": "",
            "workflow_path": "",
            "error": "CLONE_NOT_FOUND",
        })
        continue

    n_clone_found += 1

    workflows_dir = repo_root / ".github" / "workflows"
    if not workflows_dir.exists():
        n_no_workflows_dir += 1
        rows.append({
            "full_name": owner_repo,
            "repo_root": str(repo_root),
            "workflow_path": "",
            "error": "NO_WORKFLOWS_DIR",
        })
        continue

    wf_files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
    if not wf_files:
        rows.append({
            "full_name": owner_repo,
            "repo_root": str(repo_root),
            "workflow_path": "",
            "error": "NO_WORKFLOWS",
        })
        continue

    for wf in wf_files:
        n_workflow_files += 1
        try:
            rec = analyze_ci_yaml_file(wf, full_name=owner_repo, repo_root=repo_root)

            # Ensure required fields always exist
            rec = dict(rec) if isinstance(rec, dict) else {"full_name": owner_repo}

            rec["full_name"] = owner_repo
            rec["repo_root"] = str(repo_root)
            rec["workflow_path"] = wf.relative_to(repo_root).as_posix()
            rec["error"] = ""
        except Exception as e:
            rec = {
                "full_name": owner_repo,
                "repo_root": str(repo_root),
                "workflow_path": wf.relative_to(repo_root).as_posix(),
                "error": f"{type(e).__name__}: {e}",
            }
        rows.append(rec)

out = pd.DataFrame(rows)
out.to_csv(OUT, index=False, encoding="utf-8-sig")

print(f"\nWrote: {OUT}")
print(f"Rows written: {len(out)}")
print("Summary:")
print(f"  URLs total:        {n_total_urls}")
print(f"  URLs parsed:       {n_parsed}")
print(f"  Clone found:       {n_clone_found}")
print(f"  Clone NOT found:   {n_clone_missing}")
print(f"  No workflows dir:  {n_no_workflows_dir}")
print(f"  Workflow files:    {n_workflow_files}")
