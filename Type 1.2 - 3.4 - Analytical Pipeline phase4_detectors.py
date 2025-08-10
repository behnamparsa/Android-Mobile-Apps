# -*- coding: utf-8 -*-
"""
Phase 4 — Detectors (Option 2) — upgraded to scan full files for device setup.
Turns signals into findings + repo aggregates.
"""

import os
import re
import pandas as pd

from Type_1_utils_pipeline import (
    OUTPUT_DIR,
    read_text_safe,
    RE_TRIG_UNIT_GRADLE,
    RE_TRIG_INSTRU_GRADLE,
    RE_TRIG_ADB,
    RE_TRIG_CLOUD,
    RE_TRIG_GRADLE_MODULE,
    RE_DEVICE_EMULATOR,
    RE_DEVICE_GMD,
    RE_DEVICE_CLOUD,
    RE_API_LEVEL,
    RE_SYSTEM_IMAGE,
)

GHA_EXPR_FULL = re.compile(r"^\s*\${{\s*[^}]+}}\s*$")

def main():
    sig_csv = os.path.join(OUTPUT_DIR, "phase3_signals.csv")
    df = pd.read_csv(sig_csv)

    findings = []
    agg = {}
    file_cache = {}

    def full_text(path: str) -> str:
        if path not in file_cache:
            file_cache[path] = read_text_safe(path)
        return file_cache[path]

    def agg_get(owner, repo):
        k = (owner, repo)
        if k not in agg:
            agg[k] = {
                "owner": owner, "repo": repo,
                "Instru_T_Device_Setup": "NO",
                "Instru_T_Trigger": "NO",
                "trigger_commands": set(),
                "gradle_modules": set(),
                "device_setup_types": set(),
                "api_levels": set(),
                "system_image_sources": set(),
                "arch": set(),
                "devices": set(),
                "actions": set(),
                "unit_tests_ci_triggered": "NO",
                "unit_tests_configured": "NO"
            }
        return agg[k]

    for _, r in df.iterrows():
        owner, repo = r["owner"], r["repo"]
        cur = agg_get(owner, repo)

        # ----- Device Setup: scan the FULL FILE once -----
        src = str(r.get("source_file", ""))
        full = full_text(src) if src else ""

        if full:
            if RE_DEVICE_EMULATOR.search(full):
                cur["Instru_T_Device_Setup"] = "YES"; cur["device_setup_types"].add("EMULATOR")
            if RE_DEVICE_GMD.search(full):
                cur["Instru_T_Device_Setup"] = "YES"; cur["device_setup_types"].add("GMD")
            if RE_DEVICE_CLOUD.search(full):
                cur["Instru_T_Device_Setup"] = "YES"; cur["device_setup_types"].add("CLOUD_LAB")

            for m in RE_API_LEVEL.finditer(full):
                cur["api_levels"].add(m.group(1))
            for m in RE_SYSTEM_IMAGE.finditer(full):
                cur["system_image_sources"].add(m.group(1))

        # ----- Triggers from RUN_CMD rows -----
        if r.get("signal_type") == "RUN_CMD":
            cmd = str(r.get("value","")).strip()
            if not cmd or GHA_EXPR_FULL.fullmatch(cmd):
                continue  # skip dynamic-only placeholders

            # INSTRUMENTATION triggers only
            if RE_TRIG_INSTRU_GRADLE.search(cmd) or RE_TRIG_ADB.search(cmd) or RE_TRIG_CLOUD.search(cmd):
                cur["Instru_T_Trigger"] = "YES"
                cur["trigger_commands"].add(cmd)
                for m in RE_TRIG_GRADLE_MODULE.finditer(cmd):
                    cur["gradle_modules"].add(m.group(1))

            # UNIT test triggers (separate; do NOT flip Instru_T_Trigger)
            if RE_TRIG_UNIT_GRADLE.search(cmd):
                cur["unit_tests_ci_triggered"] = "YES"
                findings.append({
                    "owner": owner, "repo": repo, "finding_type": "UNIT_TEST_TRIGGER",
                    "key": "cmd", "value": cmd, "source_file": r["source_file"],
                    "flat_filename": r["flat_filename"], "relative_path": r["relative_path"],
                    "ci_platform": r["ci_platform"], "html_url": r["html_url"]
                })

        # ----- Unit test configuration (from Phase 3 signals) -----
        if r.get("signal_type") == "GRADLE_TEST_CFG":
            cur["unit_tests_configured"] = "YES"
            findings.append({
                "owner": owner, "repo": repo, "finding_type": "UNIT_TEST_CONFIG",
                "key": "gradle_test_cfg", "value": str(r.get("value","")), "source_file": r["source_file"],
                "flat_filename": r["flat_filename"], "relative_path": r["relative_path"],
                "ci_platform": r["ci_platform"], "html_url": r["html_url"]
            })

    # Flatten aggregates
    agg_rows = []
    for (owner, repo), v in agg.items():
        agg_rows.append({
            "owner": owner, "repo": repo,
            "Instru_T_Device_Setup": v["Instru_T_Device_Setup"],
            "Instru_T_Trigger": v["Instru_T_Trigger"],
            "trigger_commands": ";".join(sorted(v["trigger_commands"])),
            "gradle_modules": ";".join(sorted(v["gradle_modules"])),
            "device_setup_types": ";".join(sorted(v["device_setup_types"])),
            "api_levels": ";".join(sorted(v["api_levels"])),
            "system_image_sources": ";".join(sorted(v["system_image_sources"])),
            "arch": ";".join(sorted(v["arch"])),
            "devices": ";".join(sorted(v["devices"])),
            "actions": ";".join(sorted(v["actions"])),
            "unit_tests_ci_triggered": v["unit_tests_ci_triggered"],
            "unit_tests_configured": v["unit_tests_configured"],
        })

    pd.DataFrame(findings).to_csv(os.path.join(OUTPUT_DIR, "phase4_findings.csv"), index=False)
    pd.DataFrame(agg_rows).to_csv(os.path.join(OUTPUT_DIR, "phase4_repo_agg.csv"), index=False)
    print(f"✅ Phase 4 complete. Findings: {len(findings)}, Aggregates: {len(agg_rows)}")

if __name__ == "__main__":
    main()
