
# -*- coding: utf-8 -*-
"""
Phase 3 — Parsers (Layered)
"""

from utils_pipeline import OUTPUT_DIR, read_text_safe, re, RE_GRADLE_TEST_CFG
import pandas as pd, os

def infer_kind(saved_to: str, ci_platform: str) -> str:
    n = saved_to.lower()
    if n.endswith((".yml",".yaml")): return "yaml"
    if n.endswith((".json",)): return "json"
    if n.endswith((".gradle",".gradle.kts","settings.gradle","settings.gradle.kts","gradle.properties")): return "gradle"
    if n.endswith((".sh",".bash",".zsh",".ksh",".bat",".cmd",".ps1",".psm1",".psd1")) or "shell" in ci_platform: return "shell"
    return "other"

def main():
    cfg_path = os.path.join(OUTPUT_DIR, "phase2_config_files.csv")
    df = pd.read_csv(cfg_path)

    rows = []
    for _, r in df.iterrows():
        text = read_text_safe(r["saved_to"])
        if not text: continue
        kind = infer_kind(r["saved_to"], r.get("ci_platform",""))

        # RUN_CMD lines
        for m in re.finditer(r"(^\s*run:\s*(.+)$)|(^\s*(./gradlew\b.+)$)|(^\s*(adb\s+shell\s+am\s+instrument.+)$)", text, flags=re.IGNORECASE|re.MULTILINE):
            cmd = m.group(2) or m.group(4) or m.group(6)
            if cmd:
                rows.append({
                    "owner": r["owner"], "repo": r["repo"],
                    "source_file": r["saved_to"], "flat_filename": r["flat_filename"],
                    "relative_path": r["relative_path"], "ci_platform": r["ci_platform"], "html_url": r["html_url"],
                    "signal_type": "RUN_CMD", "key": "cmd", "value": cmd.strip()
                })

        # ACTION INPUT / MATRIX: api-level, arch
        for m in re.finditer(r"(api-?level|api_level)\s*[:=]\s*([0-9]+)", text, flags=re.IGNORECASE):
            rows.append({
                "owner": r["owner"], "repo": r["repo"], "source_file": r["saved_to"],
                "flat_filename": r["flat_filename"], "relative_path": r["relative_path"],
                "ci_platform": r["ci_platform"], "html_url": r["html_url"],
                "signal_type": "ACTION_INPUT", "key": "api_level", "value": m.group(2)
            })

        # Gradle unit-test config
        if kind == "gradle":
            for m in RE_GRADLE_TEST_CFG.finditer(text):
                rows.append({
                    "owner": r["owner"], "repo": r["repo"], "source_file": r["saved_to"],
                    "flat_filename": r["flat_filename"], "relative_path": r["relative_path"],
                    "ci_platform": r["ci_platform"], "html_url": r["html_url"],
                    "signal_type": "GRADLE_TEST_CFG", "key": "gradle_test_cfg", "value": m.group(1)
                })

    out_csv = os.path.join(OUTPUT_DIR, "phase3_signals.csv")
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(f"✅ Phase 3 complete. Signals: {len(rows)}")

if __name__ == "__main__":
    main()
