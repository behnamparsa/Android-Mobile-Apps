

from Type_1_utils_pipeline import load_index, OUTPUT_DIR, iter_repo_groups
import os, pandas as pd

def main():
    df = load_index()
    df.to_csv(os.path.join(OUTPUT_DIR, "phase1_index_clean.csv"), index=False)
    repo_rows = [{"owner": o, "repo": r, "n_files": len(g)} for o, r, g in iter_repo_groups(df)]
    pd.DataFrame(repo_rows).to_csv(os.path.join(OUTPUT_DIR, "phase1_repos.csv"), index=False)
    print(f"✅ Phase 1 complete. Repos: {len(repo_rows)}")

if __name__ == "__main__":
    main()
