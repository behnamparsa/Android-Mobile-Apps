
# -*- coding: utf-8 -*-
"""
Phase 5 — Normalization & Scoring (Option 2)
- Merge Phase 2 instrumentation counts with Phase 4 aggregates
- Compute tests_exist, unit_tests_status (ONLY from CI/build config), confidences
"""

from utils_pipeline import OUTPUT_DIR
import pandas as pd, os

def main():
    inv = pd.read_csv(os.path.join(OUTPUT_DIR, "phase2_test_inventory.csv"))
    agg = pd.read_csv(os.path.join(OUTPUT_DIR, "phase4_repo_agg.csv"))
    df = inv.merge(agg, on=["owner","repo"], how="outer").fillna({"native_instru_count":0,"flutter_instru_count":0})

    tests_exist = []
    conf_setup = []
    conf_trigger = []
    unit_status = []
    unit_conf = []

    for _, r in df.iterrows():
        # instrumentation tests_exist
        prelim = str(r.get("tests_exist_prelim","NO"))
        has_trigger = str(r.get("Instru_T_Trigger","NO")) == "YES"
        final_exist = prelim if prelim in ("YES","NO") else "NO"
        if final_exist == "NO" and has_trigger:
            final_exist = "UNKNOWN"
        tests_exist.append(final_exist)

        # confidence (device setup)
        setup = str(r.get("Instru_T_Device_Setup","NO")) == "YES"
        if setup and str(r.get("device_setup_types","")):
            conf_setup.append("high")
        elif setup:
            conf_setup.append("medium")
        else:
            conf_setup.append("low")

        # confidence (trigger)
        if has_trigger and str(r.get("trigger_commands","")):
            conf_trigger.append("high")
        elif has_trigger:
            conf_trigger.append("medium")
        else:
            conf_trigger.append("low")

        # unit tests status & confidence — ONLY from CI/build configs
        ut_ci = str(r.get("unit_tests_ci_triggered","NO")) == "YES"
        ut_cfg = str(r.get("unit_tests_configured","NO")) == "YES"

        if ut_ci:
            unit_status.append("YES")
            unit_conf.append("high")
        elif ut_cfg:
            unit_status.append("LIKELY")
            unit_conf.append("medium")
        else:
            unit_status.append("UNKNOWN")
            unit_conf.append("low")

    df["tests_exist"] = tests_exist
    df["confidence_device_setup"] = conf_setup
    df["confidence_trigger"] = conf_trigger
    df["unit_tests_status"] = unit_status
    df["confidence_unit_tests"] = unit_conf

    df.to_csv(os.path.join(OUTPUT_DIR, "phase5_repo_normalized.csv"), index=False)
    print(f"✅ Phase 5 complete. Rows: {len(df)}")

if __name__ == "__main__":
    main()
