
# -*- coding: utf-8 -*-
"""
Phase 2 — File Map, Content Loader, and Test Inventory (Option 2)
- Build per-repo config file list
- Count INSTRUMENTATION tests ONLY from All_Test_Files
- Emit: phase2_config_files.csv, phase2_test_inventory.csv
"""

from Type_1_utils_pipeline import load_index, OUTPUT_DIR, iter_repo_groups, count_instrumentation_from_all_test_files
import os, pandas as pd

def main():
    df = load_index()
    config_records = []
    inv_records = []

    for owner, repo, g in iter_repo_groups(df):
        g_cfg = g[g["bucket"]=="All_Config_Files"].copy()
        for _, r in g_cfg.iterrows():
            config_records.append({
                "owner": owner, "repo": repo,
                "flat_filename": r["flat_filename"],
                "saved_to": r["saved_to"],
                "relative_path": r["relative_path"],
                "ci_platform": r["ci_platform"],
                "html_url": r["html_url"]
            })

        inv = count_instrumentation_from_all_test_files(owner, repo)
        tests_exist_prelim = "YES" if (inv["native_instru"] + inv["flutter_instru"]) > 0 else "NO"
        inv_records.append({
            "owner": owner, "repo": repo,
            "native_instru_count": inv["native_instru"],
            "flutter_instru_count": inv["flutter_instru"],
            # Unit test counts are UNKNOWN by scope; keep as 0 purely for schema continuity
            "native_unit_count": 0,
            "modules_inferred": "",  # not available without paths
            "tests_exist_prelim": tests_exist_prelim
        })

    pd.DataFrame(config_records).to_csv(os.path.join(OUTPUT_DIR, "phase2_config_files.csv"), index=False)
    pd.DataFrame(inv_records).to_csv(os.path.join(OUTPUT_DIR, "phase2_test_inventory.csv"), index=False)
    print(f"✅ Phase 2 complete.")

if __name__ == "__main__":
    main()
