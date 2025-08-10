
# -*- coding: utf-8 -*-
"""
Shared utilities for the Android test analysis pipeline (Option 2).
UNIT TESTS are inferred ONLY from CI/build configs (no unit test files counted).
"""

from __future__ import annotations
import os, re, json, csv, sys
from pathlib import Path
from typing import Dict, List, Tuple, Iterable, Optional
import pandas as pd

# ====== PATHS (edit if needed) ======
BASE_IN = r"C:\Android Mobile App\Step2_Clone_Repo\Type_1\Aug_8"
ALL_CONFIG_INDEX = os.path.join(BASE_IN, "All_Config_Index.csv")
ALL_CONFIG_FILES_DIR = os.path.join(BASE_IN, "All_Config_Files")
ALL_TEST_FILES_DIR = os.path.join(BASE_IN, "All_Test_Files")  # contains instrumentation tests only

OUTPUT_DIR = r"C:\Android Mobile App\Step3_Instr_Testing_Analysis\Type_1\Aug_10"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ====== GENERAL HELPERS ======
REQ_COLS = [
    "owner","repo","repo_url","default_branch","commit_sha","relative_path","filename",
    "flat_filename","ci_platform","html_url","saved_to","bucket","ci_root"
]

def load_index() -> pd.DataFrame:
    df = pd.read_csv(ALL_CONFIG_INDEX)
    df.columns = [c.strip() for c in df.columns]
    missing = [c for c in REQ_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"All_Config_Index.csv is missing columns: {missing}")
    # Clean
    for c in ["owner","repo","relative_path","filename","flat_filename","ci_platform","bucket","html_url","saved_to"]:
        df[c] = df[c].astype(str).str.strip()
    return df

def read_text_safe(path: str, max_bytes: int = 1_500_000) -> str:
    try:
        size = os.path.getsize(path)
        if size <= max_bytes:
            with open(path, "rb") as f:
                b = f.read()
            return b.decode("utf-8", errors="ignore").replace("\r\n","\n")
        else:
            head = b""
            tail = b""
            with open(path, "rb") as f:
                head = f.read(max_bytes // 2)
                f.seek(max(0, size - max_bytes // 2))
                tail = f.read(max_bytes // 2)
            return (head + b"\n...\n" + tail).decode("utf-8", errors="ignore").replace("\r\n","\n")
    except Exception:
        return ""

def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def iter_repo_groups(df: pd.DataFrame):
    for (owner, repo), g in df.groupby(["owner","repo"]):
        yield owner, repo, g.sort_values("relative_path", kind="stable")

def count_instrumentation_from_all_test_files(owner: str, repo: str) -> dict:
    """
    All_Test_Files contains only instrumentation tests per current clone scope.
    Count native (kt/java) and flutter (dart) as instrumentation tests.
    No unit test counting here.
    """
    prefix = f"{owner.lower()}.{repo.lower()}__"
    native_instru = 0
    flutter_instru = 0

    if not os.path.isdir(ALL_TEST_FILES_DIR):
        return {"native_instru":0, "flutter_instru":0}

    for name in os.listdir(ALL_TEST_FILES_DIR):
        lname = name.lower()
        if not lname.startswith(prefix):
            continue
        if lname.endswith((".kt", ".java")):
            native_instru += 1
        elif lname.endswith(".dart"):
            flutter_instru += 1

    return {"native_instru": native_instru, "flutter_instru": flutter_instru}

# ====== PATTERNS ======
RE_TRIG_GRADLE = re.compile(r"\bgradlew?\b[^\n]*\b(?::\S+:)?(connected(?:AndroidTest|Check)|allDevices(?:Test|Check)|test(?:\w*UnitTest)?)\b", re.IGNORECASE)
RE_TRIG_GRADLE_MODULE = re.compile(r"\bgradlew?\b[^\n]*\b(:\w+(?::\w+)*)\b", re.IGNORECASE)
RE_TRIG_ADB = re.compile(r"\badb\s+shell\s+am\s+instrument\b", re.IGNORECASE)
RE_TRIG_CLOUD = re.compile(r"\b(gcloud\s+firebase\s+test\s+android\s+run|flank\s+android\s+run|marathon)\b", re.IGNORECASE)

RE_DEVICE_EMULATOR = re.compile(r"\b(sdkmanager|avdmanager|emulator|api-?level|x86(?:_64)?|arm(?:64)?)\b", re.IGNORECASE)
RE_DEVICE_GMD = re.compile(r"(testOptions\.managedDevices|managedVirtualDevice|deviceGroups|apiLevel|systemImageSource)", re.IGNORECASE)
RE_DEVICE_CLOUD = re.compile(r"(gcloud\s+firebase\s+test\s+android\s+run|saucectl)", re.IGNORECASE)

RE_API_LEVEL = re.compile(r"api-?level[:= ]\s*(\d+)", re.IGNORECASE)
RE_SYSTEM_IMAGE = re.compile(r"(google_apis|aosp_atd|google_atd|google_apis_playstore)", re.IGNORECASE)

RE_GRADLE_TEST_CFG = re.compile(r"\b(android\.testOptions\.unitTests\.[^\n]+|testImplementation|testRuntimeOnly)\b", re.IGNORECASE)
