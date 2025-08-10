# -*- coding: utf-8 -*-
"""
Phase 3 — Parsers (Layered) — upgraded RUN_CMD coverage and de-noising.
Emits:
  - RUN_CMD rows (deduped, comments & dynamic-only expressions filtered)
  - ACTION_INPUT (api_level / arch hints)
  - GRADLE_TEST_CFG (unit test deps/options)
"""

import os
import re
import pandas as pd

from Type_1_utils_pipeline import (
    OUTPUT_DIR,
    read_text_safe,
)

GHA_EXPR_FULL = re.compile(r"^\s*\${{\s*[^}]+}}\s*$")

# simple comment detectors (YAML/Shell/Batch/PS)
def is_commented_line(line: str) -> bool:
    s = line.lstrip()
    return s.startswith("#") or s.startswith("//") or s.startswith("::")

GRADLE_LINE = re.compile(r"\bgradle(?:w(?:\.bat)?)?\b", re.IGNORECASE)
ADB_LINE    = re.compile(r"\badb\s+shell\s+am\s+instrument\b", re.IGNORECASE)
CLOUD_LINE  = re.compile(r"\b(gcloud\s+firebase\s+test\s+android\s+run|flank\s+android\s+run|marathon)\b", re.IGNORECASE)

API_LEVEL_KV = re.compile(r"(api-?level|api_level)\s*[:=]\s*([0-9]+)", re.IGNORECASE)
ARCH_KV      = re.compile(r"\b(arch|architecture)\b\s*[:=]\s*([A-Za-z0-9_+-]+)", re.IGNORECASE)

from Type_1_utils_pipeline import RE_GRADLE_TEST_CFG  # keep using your utils pattern

def main():
    cfg_path = os.path.join(OUTPUT_DIR, "phase2_config_files.csv")
    df = pd.read_csv(cfg_path)

    rows = []
    for _, r in df.iterrows():
        src = r["saved_to"]
        text = read_text_safe(src)
        if not text:
            continue

        # ---- RUN_CMD extraction ----
        emitted = set()
        for raw in text.splitlines():
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            if is_commented_line(line):
                continue
            if GHA_EXPR_FULL.fullmatch(line.strip()):
                continue  # dynamic-only expression, no literal command

            if GRADLE_LINE.search(line) or ADB_LINE.search(line) or CLOUD_LINE.search(line):
                cmd = line.strip()
                key = (src, cmd)
                if key in emitted:
                    continue
                emitted.add(key)
                rows.append({
                    "owner": r["owner"], "repo": r["repo"],
                    "source_file": src, "flat_filename": r["flat_filename"],
                    "relative_path": r["relative_path"], "ci_platform": r["ci_platform"], "html_url": r["html_url"],
                    "signal_type": "RUN_CMD", "key": "cmd", "value": cmd
                })

        # ---- ACTION_INPUT / MATRIX hints ----
        for m in API_LEVEL_KV.finditer(text):
            rows.append({
                "owner": r["owner"], "repo": r["repo"], "source_file": src,
                "flat_filename": r["flat_filename"], "relative_path": r["relative_path"],
                "ci_platform": r["ci_platform"], "html_url": r["html_url"],
                "signal_type": "ACTION_INPUT", "key": "api_level", "value": m.group(2)
            })
        for m in ARCH_KV.finditer(text):
            rows.append({
                "owner": r["owner"], "repo": r["repo"], "source_file": src,
                "flat_filename": r["flat_filename"], "relative_path": r["relative_path"],
                "ci_platform": r["ci_platform"], "html_url": r["html_url"],
                "signal_type": "ACTION_INPUT", "key": "arch", "value": m.group(2)
            })

        # ---- Gradle unit-test configuration signals ----
        for m in RE_GRADLE_TEST_CFG.finditer(text):
            rows.append({
                "owner": r["owner"], "repo": r["repo"], "source_file": src,
                "flat_filename": r["flat_filename"], "relative_path": r["relative_path"],
                "ci_platform": r["ci_platform"], "html_url": r["html_url"],
                "signal_type": "GRADLE_TEST_CFG", "key": "gradle_test_cfg", "value": m.group(1)
            })

    out_csv = os.path.join(OUTPUT_DIR, "phase3_signals.csv")
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(f"✅ Phase 3 complete. Signals: {len(rows)}")

if __name__ == "__main__":
    main()
