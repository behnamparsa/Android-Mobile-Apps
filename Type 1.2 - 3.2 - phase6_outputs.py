
# -*- coding: utf-8 -*-
"""
Phase 6 — Final Outputs
"""

from utils_pipeline import OUTPUT_DIR
import pandas as pd, os

def main():
    df = pd.read_csv(os.path.join(OUTPUT_DIR, "phase5_repo_normalized.csv"))
    fpath = os.path.join(OUTPUT_DIR, "phase4_findings.csv")
    find = pd.read_csv(fpath) if os.path.exists(fpath) else pd.DataFrame()

    cols = [
        "owner","repo","native_instru_count","flutter_instru_count",
        "tests_exist","Instru_T_Device_Setup","Instru_T_Trigger",
        "device_setup_types","api_levels","system_image_sources",
        "trigger_commands","gradle_modules",
        "unit_tests_ci_triggered","unit_tests_configured","unit_tests_status",
        "confidence_device_setup","confidence_trigger","confidence_unit_tests"
    ]
    keep = [c for c in cols if c in df.columns]
    df[keep].to_csv(os.path.join(OUTPUT_DIR, "repo_summary.csv"), index=False)

    out_find = os.path.join(OUTPUT_DIR, "findings.csv")
    if not find.empty:
        find.to_csv(out_find, index=False)
    else:
        pd.DataFrame(columns=["owner","repo","finding_type","key","value","source_file","flat_filename","relative_path","ci_platform","html_url"]).to_csv(out_find, index=False)

    print("✅ Phase 6 complete. Outputs written.")

if __name__ == "__main__":
    main()
