
# -*- coding: utf-8 -*-
"""
Phase 4 — Detectors (Option 2)
"""

from utils_pipeline import OUTPUT_DIR, re, RE_TRIG_GRADLE, RE_TRIG_ADB, RE_TRIG_CLOUD, RE_TRIG_GRADLE_MODULE, \
    RE_DEVICE_EMULATOR, RE_DEVICE_GMD, RE_DEVICE_CLOUD, RE_API_LEVEL, RE_SYSTEM_IMAGE
import pandas as pd, os

def main():
    sig_csv = os.path.join(OUTPUT_DIR, "phase3_signals.csv")
    df = pd.read_csv(sig_csv)

    findings = []
    agg = {}

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
        text = str(r.get("value",""))
        owner, repo = r["owner"], r["repo"]
        cur = agg_get(owner, repo)

        if r["signal_type"] == "RUN_CMD":
            cmd = text

            # instrumentation triggers
            if RE_TRIG_GRADLE.search(cmd) or " connectedAndroidTest" in cmd or RE_TRIG_ADB.search(cmd) or RE_TRIG_CLOUD.search(cmd):
                cur["Instru_T_Trigger"] = "YES"
                cur["trigger_commands"].add(cmd)
                for m in RE_TRIG_GRADLE_MODULE.finditer(cmd):
                    cur["gradle_modules"].add(m.group(1))

            # unit test triggers (CI/build)
            if re.search(r"\bgradlew?\b[^\n]*\btest(?:\w*UnitTest)?\b", cmd, flags=re.IGNORECASE) or re.search(r"\bgradlew?\b[^\n]*\bcheck\b", cmd, flags=re.IGNORECASE):
                cur["unit_tests_ci_triggered"] = "YES"
                findings.append({
                    "owner": owner, "repo": repo, "finding_type": "UNIT_TEST_TRIGGER",
                    "key": "cmd", "value": cmd, "source_file": r["source_file"],
                    "flat_filename": r["flat_filename"], "relative_path": r["relative_path"],
                    "ci_platform": r["ci_platform"], "html_url": r["html_url"]
                })

        # device setup indicators
        if RE_DEVICE_EMULATOR.search(text): cur["Instru_T_Device_Setup"] = "YES"; cur["device_setup_types"].add("EMULATOR")
        if RE_DEVICE_GMD.search(text): cur["Instru_T_Device_Setup"] = "YES"; cur["device_setup_types"].add("GMD")
        if RE_DEVICE_CLOUD.search(text): cur["Instru_T_Device_Setup"] = "YES"; cur["device_setup_types"].add("CLOUD_LAB")

        for m in RE_API_LEVEL.finditer(text): cur["api_levels"].add(m.group(1))
        for m in RE_SYSTEM_IMAGE.finditer(text): cur["system_image_sources"].add(m.group(1))

        if r["signal_type"] == "GRADLE_TEST_CFG":
            cur["unit_tests_configured"] = "YES"
            findings.append({
                "owner": owner, "repo": repo, "finding_type": "UNIT_TEST_CONFIG",
                "key": "gradle_test_cfg", "value": text, "source_file": r["source_file"],
                "flat_filename": r["flat_filename"], "relative_path": r["relative_path"],
                "ci_platform": r["ci_platform"], "html_url": r["html_url"]
            })

    # Flatten
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
