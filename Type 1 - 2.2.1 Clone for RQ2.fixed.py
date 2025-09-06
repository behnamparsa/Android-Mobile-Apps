
#!/usr/bin/env python3
"""
RQ2 mining toolkit
- Step 0: clone repos from a CSV of repo URLs
- Step 1: mine commit-by-commit config "snapshots" (normalized fields)
- Step 2: produce Configuration Change Episodes (CCEs) + labels (Fix/Upgrade/Enhancement, with tie-break)
- Step 3: combine per-repo JSONLs into master CSV/Parquet

CSV format:
    URL_List.csv with column "repo_url"

Notes:
- Uses `git` CLI via subprocess (no GitHub API required).
- Focuses on GitHub/GitLab/Circle/Bitrise/Azure YAMLs, Gradle Groovy/KTS, and helper scripts.
- Heuristic parsers are conservative; extend `YAML_KEYS`, `GRADLE_PATTERNS`, and `THIRDPARTY_HINTS` as needed.

Author: you
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

try:
    import yaml
except ImportError:
    yaml = None

# -----------------------------
# Config
# -----------------------------
CI_FILES = [
    ".github/workflows", ".gitlab-ci.yml", ".circleci/config.yml",
    "bitrise.yml", "azure-pipelines.yml"
]
GRADLE_FILES = [
    "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "gradle.properties", "gradle/wrapper/gradle-wrapper.properties"
]
SCRIPT_HINTS = ["script", "run", "bash", "sh", "python"]

# Regex helpers for Gradle & YAML
RE_INT = re.compile(r"\d+")
RE_VERSION = re.compile(r"\b(\d+(?:\.\d+){0,3})\b")

# Keys to scan within YAML-like dicts
YAML_KEYS = {
    "api": ["api-level", "apilevel", "api_level"],
    "abi": ["abi", "arch", "cpu", "abi_filters", "abi-filter"],
    "system_image": ["system-image", "target", "systemimage"],
    "device": ["device", "avd-name", "avd", "device-profile", "model", "hardwareProfile"],
    "orchestrator": ["orchestrator", "android-test-orchestrator", "use-orchestrator"],
    "wait": ["wait-for-boot", "wait_for_boot"],
    "timeouts": ["emulator-boot-timeout", "timeout", "test-timeout", "emulator_timeout"],
    "retries": ["retry", "retries", "max-retries"],
    "matrix": ["matrix", "strategy"],
    "runner_os": ["runs-on", "machine", "image"],
    "jdk": ["java-version", "jdk", "java", "distribution"],
    "invocation": ["run", "gradle_args", "gradlew_args", "task", "tasks"],
    "thirdparty": ["browserstack", "saucelabs", "firebase", "bitbar", "kobiton"],
}

# Heuristic Gradle patterns
GRADLE_PATTERNS = {
    "managed_devices_block": re.compile(r"managedDevices\s*\{", re.IGNORECASE),
    "managed_device_api": re.compile(r"apiLevel\s*=\s*(\d+)", re.IGNORECASE),
    "managed_device_abi": re.compile(r"abi\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE),
    "managed_device_name": re.compile(r"device\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE),
    "orchestrator": re.compile(r"execution\s+['\"]ANDROIDX_TEST_ORCHESTRATOR['\"]", re.IGNORECASE),
    "num_shards": re.compile(r"(?:numShards|shardCount)\s*=?\s*(\d+)", re.IGNORECASE),
    "agp_version": re.compile(r"com\.android\.tools\.build:gradle:([0-9][^'\"\s)]+)"),
    "invocation_connected": re.compile(r"\bconnectedAndroidTest\b"),
}

# Third-party runner hints (non-emulator labs)
THIRDPARTY_HINTS = ["browserstack", "saucelabs", "firebase", "testlab", "devicefarm", "bitbar"]

# -----------------------------
# Utils
# -----------------------------
def sh(cmd: List[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)

def safe_jsonl_write(path: Path, rows: List[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def read_repo_urls(csv_path: Path) -> List[str]:
    urls = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            u = (row.get("repo_url") or "").strip()
            if u:
                urls.append(u)
    return urls

def repo_dir_name_from_url(url: str) -> str:
    # e.g., https://github.com/owner/name(.git) -> owner__name
    base = url.split("//")[-1]
    parts = base.split("/")
    if len(parts) >= 3:
        owner = parts[-2]
        name = parts[-1].replace(".git", "")
        return f"{owner}__{name}"
    # fallback
    name = base.replace("/", "__").replace(".git", "")
    return name

def ensure_cloned(url: str, dest_root: Path) -> Path:
    dest_root.mkdir(parents=True, exist_ok=True)
    d = dest_root / repo_dir_name_from_url(url)
    if d.exists() and (d / ".git").exists():
        # fetch updates
        try:
            sh(["git", "fetch", "--all", "--tags", "--prune"], cwd=d)
        except Exception:
            pass
        return d
    sh(["git", "clone", "--no-tags", "--filter=blob:none", "--recurse-submodules=no", url, str(d)])
    return d

def get_total_commits(repo_dir: Path) -> int:
    cp = sh(["git", "rev-list", "--all", "--count"], cwd=repo_dir)
    return int(cp.stdout.strip())

def list_relevant_commits(repo_dir: Path) -> List[Tuple[str, int, List[str]]]:
    """
    Return list of (sha, timestamp, changed_paths) for commits that touched relevant surfaces.
    """
    # `git log --name-only --pretty=%H%x09%ct`
    cp = sh(["git", "log", "--all", "--name-only", "--pretty=%H\t%ct"], cwd=repo_dir)
    lines = cp.stdout.splitlines()
    results = []
    sha, ts = None, None
    changed = []
    def touched_relevant(paths: List[str]) -> bool:
        for p in paths:
            p = p.strip()
            if not p:
                continue
            # CI files
            if p.startswith(".github/workflows/") or p in [".gitlab-ci.yml", ".circleci/config.yml", "bitrise.yml", "azure-pipelines.yml"]:
                return True
            # Gradle
            if p.endswith("build.gradle") or p.endswith("build.gradle.kts") or p.endswith("settings.gradle") or p.endswith("settings.gradle.kts") or p.endswith("gradle.properties") or p.endswith("gradle/wrapper/gradle-wrapper.properties"):
                return True
            # scripts (heuristic)
            if p.endswith(".sh") or p.endswith(".py") or "script" in p.lower():
                return True
        return False

    for line in lines:
        if "\t" in line and len(line.split("\t")[0]) == 40:
            # new commit line
            if sha is not None and touched_relevant(changed):
                results.append((sha, ts, changed))
            sha, ts_s = line.split("\t", 1)
            ts = int(ts_s)
            changed = []
        else:
            if line.strip():
                changed.append(line.strip())
    if sha is not None and touched_relevant(changed):
        results.append((sha, ts, changed))
    return list(reversed(results))  # oldest → newest

def git_show(repo_dir: Path, sha: str, path: str) -> Optional[str]:
    try:
        cp = sh(["git", "show", f"{sha}:{path}"], cwd=repo_dir)
        return cp.stdout
    except subprocess.CalledProcessError:
        return None

# -----------------------------
# Field extraction
# -----------------------------
def _extract_from_yaml_text(text: str) -> Dict[str, Any]:
    if yaml is None:
        return {}
    out = {
        "api_levels": set(),
        "abis": set(),
        "system_images": set(),
        "device_profiles": set(),
        "orchestrator": None,
        "wait_for_boot": None,
        "timeouts": {},
        "retries": None,
        "matrix_axes": set(),
        "runner_os": None,
        "jdk": None,
        "invocation_hints": [],
        "thirdparty_refs": set(),
    }

    # Try parse; if it fails, fall back to key/regex scans
    docs = []
    try:
        docs = list(yaml.safe_load_all(text))
    except Exception:
        docs = []

    def scan_obj(obj, path=""):
        if isinstance(obj, dict):
            # capture keys
            for k, v in obj.items():
                lk = str(k).lower()
                # api
                if lk in (x.lower() for x in YAML_KEYS["api"]):
                    if isinstance(v, list):
                        for x in v:
                            if isinstance(x, (int, str)) and RE_INT.search(str(x)):
                                out["api_levels"].add(int(RE_INT.search(str(x)).group()))
                    elif isinstance(v, (int, str)):
                        m = RE_INT.search(str(v))
                        if m:
                            out["api_levels"].add(int(m.group()))
                # abi
                if lk in (x.lower() for x in YAML_KEYS["abi"]):
                    vals = []
                    if isinstance(v, list):
                        vals = v
                    else:
                        vals = [v]
                    for x in vals:
                        if isinstance(x, str):
                            out["abis"].add(x.strip())
                # system image / target
                if lk in (x.lower() for x in YAML_KEYS["system_image"]):
                    vals = []
                    if isinstance(v, list):
                        vals = v
                    else:
                        vals = [v]
                    for x in vals:
                        if isinstance(x, str):
                            out["system_images"].add(x.strip())
                # device
                if lk in (x.lower() for x in YAML_KEYS["device"]):
                    vals = v if isinstance(v, list) else [v]
                    for x in vals:
                        if isinstance(x, str):
                            out["device_profiles"].add(x.strip())
                # orchestrator
                if lk in (x.lower() for x in YAML_KEYS["orchestrator"]):
                    if isinstance(v, bool):
                        out["orchestrator"] = v
                    elif isinstance(v, str):
                        out["orchestrator"] = v.lower() in ("1", "true", "yes", "on")
                # wait-for-boot
                if lk in (x.lower() for x in YAML_KEYS["wait"]):
                    if isinstance(v, bool):
                        out["wait_for_boot"] = v
                    elif isinstance(v, str):
                        out["wait_for_boot"] = v.lower() in ("1", "true", "yes", "on")
                # timeouts
                if lk in (x.lower() for x in YAML_KEYS["timeouts"]):
                    # normalize to seconds if you can parse int
                    if isinstance(v, (int, str)):
                        m = RE_INT.search(str(v))
                        if m:
                            out["timeouts"][lk] = int(m.group())
                # retries
                if lk in (x.lower() for x in YAML_KEYS["retries"]):
                    if isinstance(v, (int, str)) and RE_INT.search(str(v)):
                        out["retries"] = int(RE_INT.search(str(v)).group())
                # matrix/strategy
                if lk in (x.lower() for x in YAML_KEYS["matrix"]):
                    if isinstance(v, dict):
                        for mk in v.keys():
                            out["matrix_axes"].add(str(mk))
                # runner OS
                if lk in (x.lower() for x in YAML_KEYS["runner_os"]):
                    if isinstance(v, str):
                        out["runner_os"] = v
                # JDK
                if lk in (x.lower() for x in YAML_KEYS["jdk"]):
                    if isinstance(v, (int, str)):
                        out["jdk"] = str(v)
                # invocation hints & third-party
                if lk in (x.lower() for x in YAML_KEYS["invocation"]):
                    if isinstance(v, str):
                        out["invocation_hints"].append(v.lower())
                if any(h in lk for h in YAML_KEYS["thirdparty"]):
                    out["thirdparty_refs"].add(lk)
                scan_obj(v, path + "/" + str(k))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                scan_obj(v, path + f"/{i}")
        else:
            # scalar: look for hints (reactivecircus runner, gradle tasks, etc.)
            if isinstance(obj, str):
                s = obj.lower()
                if "reactivecircus/android-emulator-runner" in s:
                    out["invocation_hints"].append("emulator-runner")
                for h in THIRDPARTY_HINTS:
                    if h in s:
                        out["thirdparty_refs"].add(h)

    if docs:
        for d in docs:
            scan_obj(d)
    else:
        # fallback: regex/key search on raw text
        for key in YAML_KEYS["api"]:
            for m in re.finditer(rf"{key}\s*[:=]\s*['\"]?(\d+)", text, flags=re.IGNORECASE):
                out["api_levels"].add(int(m.group(1)))
        for key in YAML_KEYS["abi"]:
            for m in re.finditer(rf"{key}\s*[:=]\s*['\"]?([A-Za-z0-9_\-]+)", text, flags=re.IGNORECASE):
                out["abis"].add(m.group(1))

    # Invocation + environment classification
    invoc = None
    if any("gradle" in x or "connectedandroidtest" in x for x in out["invocation_hints"]):
        invoc = "Gradle"
    elif any("adb" in x for x in out["invocation_hints"]):
        invoc = "ADB"
    elif out["thirdparty_refs"]:
        invoc = "3P_CLI"
    # Environment
    env = "DIY"
    if out["thirdparty_refs"]:
        env = "ThirdParty"

    return {
        "api_levels": sorted(list(out["api_levels"])),
        "abis": sorted(list(out["abis"])),
        "system_images": sorted(list(out["system_images"])),
        "device_profiles": sorted(list(out["device_profiles"])),
        "orchestrator": out["orchestrator"],
        "wait_for_boot": out["wait_for_boot"],
        "timeouts": out["timeouts"],
        "retries": out["retries"],
        "matrix_axes": sorted(list(out["matrix_axes"])),
        "runner_os": out["runner_os"],
        "jdk": out["jdk"],
        "invocation": invoc,
        "environment": env,
    }

def _extract_from_gradle_text(text: str) -> Dict[str, Any]:
    out = {
        "gmd_present": bool(GRADLE_PATTERNS["managed_devices_block"].search(text)),
        "gmd_api_levels": [],
        "gmd_abis": [],
        "gmd_devices": [],
        "orchestrator": bool(GRADLE_PATTERNS["orchestrator"].search(text)),
        "num_shards": None,
        "agp_version": None,
        "invocation_connected": bool(GRADLE_PATTERNS["invocation_connected"].search(text)),
        "gradle_version": None,
    }

    out["gmd_api_levels"] = [int(m.group(1)) for m in GRADLE_PATTERNS["managed_device_api"].finditer(text)]
    out["gmd_abis"] = [m.group(1) for m in GRADLE_PATTERNS["managed_device_abi"].finditer(text)]
    out["gmd_devices"] = [m.group(1) for m in GRADLE_PATTERNS["managed_device_name"].finditer(text)]

    m = GRADLE_PATTERNS["num_shards"].search(text)
    if m:
        out["num_shards"] = int(m.group(1))

    m = GRADLE_PATTERNS["agp_version"].search(text)
    if m:
        out["agp_version"] = m.group(1)

    # gradle wrapper version
    m2 = re.search(r"distributionUrl=.*gradle-([0-9][\w\.\-]+)-bin\.zip", text)
    if m2:
        out["gradle_version"] = m2.group(1)

    return out

def merge_fields(yaml_fields: Dict[str, Any], gradle_fields: Dict[str, Any]) -> Dict[str, Any]:
    # environment: prefer GMD if present
    environment = "DIY"
    if gradle_fields.get("gmd_present"):
        environment = "GMD"
    elif yaml_fields.get("environment") == "ThirdParty":
        environment = "ThirdParty"

    # invocation
    invocation = yaml_fields.get("invocation")
    if gradle_fields.get("invocation_connected"):
        invocation = "Gradle"

    # orchestrator: True if set in either
    orchestrator = yaml_fields.get("orchestrator")
    if gradle_fields.get("orchestrator"):
        orchestrator = True if orchestrator is None else (orchestrator or True)

    # API levels, ABIs, devices
    api_levels = set(yaml_fields.get("api_levels") or [])
    api_levels.update(gradle_fields.get("gmd_api_levels") or [])

    abis = set(yaml_fields.get("abis") or [])
    abis.update(gradle_fields.get("gmd_abis") or [])

    devices = set(yaml_fields.get("device_profiles") or [])
    devices.update(gradle_fields.get("gmd_devices") or [])

    # parallelism
    parallelism = {
        "numShards": gradle_fields.get("num_shards"),
        "matrix_axes": yaml_fields.get("matrix_axes") or [],
    }

    return {
        "api_levels": sorted(list(api_levels)),
        "abis": sorted(list(abis)),
        "system_images": yaml_fields.get("system_images") or [],
        "device_profiles": sorted(list(devices)),
        "orchestrator": orchestrator,
        "wait_for_boot": yaml_fields.get("wait_for_boot"),
        "timeouts": yaml_fields.get("timeouts") or {},
        "retries": yaml_fields.get("retries"),
        "parallelism": parallelism,
        "runner_os": yaml_fields.get("runner_os"),
        "jdk": yaml_fields.get("jdk"),
        "invocation": invocation,
        "environment": environment,
        "agp_version": gradle_fields.get("agp_version"),
        "gradle_version": gradle_fields.get("gradle_version"),
    }

# -----------------------------
# Snapshot mining
# -----------------------------
def build_snapshot_for_commit(repo_dir: Path, sha: str, changed_paths: List[str]) -> Dict[str, Any]:
    yaml_fields_agg = {
        "api_levels": [], "abis": [], "system_images": [], "device_profiles": [],
        "orchestrator": None, "wait_for_boot": None, "timeouts": {}, "retries": None,
        "matrix_axes": [], "runner_os": None, "jdk": None, "invocation": None,
        "environment": "DIY",
        "thirdparty_refs": []
    }
    gradle_fields_agg = {
        "gmd_present": False, "gmd_api_levels": [], "gmd_abis": [], "gmd_devices": [],
        "orchestrator": False, "num_shards": None, "agp_version": None,
        "invocation_connected": False, "gradle_version": None
    }

    # Decide which files to read (we will read union of CI + Gradle + wrapper)
    candidate_paths = set()
    for p in changed_paths:
        p = p.strip()
        if not p:
            continue
        if p.startswith(".github/workflows/") or p in [".gitlab-ci.yml", ".circleci/config.yml", "bitrise.yml", "azure-pipelines.yml"]:
            candidate_paths.add(p)
        if p.endswith("build.gradle") or p.endswith("build.gradle.kts") or p.endswith("settings.gradle") or p.endswith("settings.gradle.kts") or p.endswith("gradle.properties") or p.endswith("gradle/wrapper/gradle-wrapper.properties"):
            candidate_paths.add(p)
        if p.endswith(".sh") or p.endswith(".py"):
            candidate_paths.add(p)

    # Parse YAML-like files
    for p in list(candidate_paths):
        if any(p.endswith(x) for x in [".yml", ".yaml"]) or p in [".gitlab-ci.yml", "bitrise.yml", ".circleci/config.yml", "azure-pipelines.yml"] or p.startswith(".github/workflows/"):
            txt = git_show(repo_dir, sha, p)
            if not txt:
                continue
            yf = _extract_from_yaml_text(txt)
            # merge into agg
            for k in ["api_levels", "abis", "system_images", "device_profiles", "matrix_axes"]:
                yaml_fields_agg[k].extend(yf.get(k) or [])
            for k in ["orchestrator", "wait_for_boot", "retries", "runner_os", "jdk", "invocation"]:
                v = yf.get(k)
                yaml_fields_agg[k] = v if yaml_fields_agg[k] in (None, [], {}) else (yaml_fields_agg[k] or v)
            yaml_fields_agg["timeouts"].update(yf.get("timeouts") or {})
            if yf.get("environment") == "ThirdParty":
                yaml_fields_agg["environment"] = "ThirdParty"

    # Parse Gradle-like files (including wrapper props)
    for p in list(candidate_paths) | {"gradle/wrapper/gradle-wrapper.properties"}:
        if p.endswith(".gradle") or p.endswith(".gradle.kts") or p.endswith("gradle-wrapper.properties") or p.endswith("gradle.properties") or p.endswith("settings.gradle") or p.endswith("settings.gradle.kts"):
            txt = git_show(repo_dir, sha, p)
            if not txt:
                continue
            gf = _extract_from_gradle_text(txt)
            gradle_fields_agg["gmd_present"] = gradle_fields_agg["gmd_present"] or gf["gmd_present"]
            for k in ["gmd_api_levels", "gmd_abis", "gmd_devices"]:
                gradle_fields_agg[k].extend(gf.get(k) or [])
            gradle_fields_agg["orchestrator"] = gradle_fields_agg["orchestrator"] or gf["orchestrator"]
            gradle_fields_agg["invocation_connected"] = gradle_fields_agg["invocation_connected"] or gf["invocation_connected"]
            # numeric fields: prefer the larger value
            if gf.get("num_shards") and (not gradle_fields_agg["num_shards"] or gf["num_shards"] > gradle_fields_agg["num_shards"]):
                gradle_fields_agg["num_shards"] = gf["num_shards"]
            # versions
            for k in ["agp_version", "gradle_version"]:
                if gf.get(k):
                    gradle_fields_agg[k] = gf[k]

    # Dedup aggregates
    yaml_fields_agg["api_levels"] = sorted(set(yaml_fields_agg["api_levels"]))
    yaml_fields_agg["abis"] = sorted(set(yaml_fields_agg["abis"]))
    yaml_fields_agg["system_images"] = sorted(set(yaml_fields_agg["system_images"]))
    yaml_fields_agg["device_profiles"] = sorted(set(yaml_fields_agg["device_profiles"]))
    yaml_fields_agg["matrix_axes"] = sorted(set(yaml_fields_agg["matrix_axes"]))

    # Merge
    merged = merge_fields(yaml_fields_agg, gradle_fields_agg)
    return merged

# -----------------------------
# CCE diffing & labeling
# -----------------------------
def diff_snapshots(prev: Dict[str, Any], curr: Dict[str, Any]) -> List[Dict[str, Any]]:
    changes = []

    def list_change(name, a, b):
        sa, sb = set(a or []), set(b or [])
        added = sorted(list(sb - sa))
        removed = sorted(list(sa - sb))
        if added or removed:
            changes.append({"field": name, "old": sorted(list(sa)), "new": sorted(list(sb)), "added": added, "removed": removed})

    def scalar_change(name, a, b):
        if a != b:
            changes.append({"field": name, "old": a, "new": b})

    list_change("api_levels", prev.get("api_levels"), curr.get("api_levels"))
    list_change("abis", prev.get("abis"), curr.get("abis"))
    list_change("system_images", prev.get("system_images"), curr.get("system_images"))
    list_change("device_profiles", prev.get("device_profiles"), curr.get("device_profiles"))
    list_change("parallelism.matrix_axes", (prev.get("parallelism") or {}).get("matrix_axes"), (curr.get("parallelism") or {}).get("matrix_axes"))

    scalar_change("parallelism.numShards", (prev.get("parallelism") or {}).get("numShards"), (curr.get("parallelism") or {}).get("numShards"))
    scalar_change("orchestrator", prev.get("orchestrator"), curr.get("orchestrator"))
    scalar_change("wait_for_boot", prev.get("wait_for_boot"), curr.get("wait_for_boot"))
    scalar_change("retries", prev.get("retries"), curr.get("retries"))
    scalar_change("runner_os", prev.get("runner_os"), curr.get("runner_os"))
    scalar_change("jdk", prev.get("jdk"), curr.get("jdk"))
    scalar_change("agp_version", prev.get("agp_version"), curr.get("agp_version"))
    scalar_change("gradle_version", prev.get("gradle_version"), curr.get("gradle_version"))
    scalar_change("invocation", prev.get("invocation"), curr.get("invocation"))
    scalar_change("environment", prev.get("environment"), curr.get("environment"))

    # timeouts: compare keys present & numeric increases
    pto, cto = prev.get("timeouts") or {}, curr.get("timeouts") or {}
    if pto != cto:
        changes.append({"field": "timeouts", "old": pto, "new": cto})

    return changes

FIX_MSG = re.compile(r"\b(fix|fixed|fixes|flaky|flake|flakiness|broken|timeout|stabil|retry|workaround|unblock)\b", re.IGNORECASE)
UPG_MSG = re.compile(r"\b(bump|upgrade|update|targetsdk|compilesdk|agp|gradle)\b", re.IGNORECASE)
ADD_MSG = re.compile(r"\b(add|enable|introduc|parallel|matrix|shard|arm64|x86_64)\b", re.IGNORECASE)

def semver_tuple(v: Optional[str]) -> Tuple[int, ...]:
    if not v:
        return tuple()
    parts = [int(x) for x in re.findall(r"\d+", v)]
    return tuple(parts)

def label_commit_changes(changes: List[Dict[str, Any]], prev: Dict[str, Any], curr: Dict[str, Any], commit_msg: str) -> Dict[str, Any]:
    """
    Decide a single primary_driver for the commit (CCE), + secondary tags.
    Tie-breaker: Fix > Upgrade > Enhancement
    """
    primary = None
    tags = set()
    rule_ids = []

    # Helpers
    def mark(p, rid):
        nonlocal primary
        if primary is None:
            primary = p
        elif p == "Fix":
            primary = "Fix"                        # Fix dominates
        elif p == "Upgrade" and primary == "Enhancement":
            primary = "Upgrade"                    # Upgrade beats Enhancement
        rule_ids.append(rid)

    # 1) FIX rules
    for ch in changes:
        f = ch["field"]
        # Missing→present api_levels
        if f == "api_levels" and not ch["old"] and ch["new"]:
            mark("Fix", "FIX_API_ADDED")
            tags.add("api-level-specified")
        # wait_for_boot added or retries/timeout increased
        if f in ("wait_for_boot", "retries", "timeouts"):
            if f == "wait_for_boot" and ch["new"] is True:
                mark("Fix", "FIX_WAIT_FOR_BOOT")
                tags.add("stability")
            if f == "retries" and (ch["old"] is None or (isinstance(ch["new"], int) and (ch["old"] or 0) < ch["new"])):
                mark("Fix", "FIX_RETRIES")
                tags.add("stability")
            if f == "timeouts":
                # any numeric increase
                try:
                    inc = any((k in ch["new"] and ch["old"].get(k, 0) < ch["new"][k]) for k in ch["new"].keys())
                except Exception:
                    inc = False
                if inc:
                    mark("Fix", "FIX_TIMEOUT_INCREASE")
                    tags.add("stability")
        # orchestrator turned on
        if f == "orchestrator" and ch["new"] is True and not ch["old"]:
            mark("Fix", "FIX_ORCHESTRATOR")
            tags.add("stability")

    if FIX_MSG.search(commit_msg or ""):
        mark("Fix", "FIX_COMMIT_MSG")
        tags.add("msg-fix")

    # 2) UPGRADE rules
    for ch in changes:
        f = ch["field"]
        if f == "api_levels":
            if ch["old"] and ch["new"]:
                if max(ch["new"]) > max(ch["old"]):
                    mark("Upgrade", "UPG_API_BUMP")
                    tags.add("api-upgrade")
        if f == "agp_version":
            if semver_tuple(ch["new"]) > semver_tuple(ch["old"]):
                mark("Upgrade", "UPG_AGP")
                tags.add("tooling-upgrade")
        if f == "gradle_version":
            if semver_tuple(ch["new"]) > semver_tuple(ch["old"]):
                mark("Upgrade", "UPG_GRADLE")
                tags.add("tooling-upgrade")
        if f == "environment":
            if ch["old"] == "DIY" and ch["new"] == "GMD":
                mark("Upgrade", "UPG_TO_GMD")
                tags.add("gmd-migration")

    if UPG_MSG.search(commit_msg or ""):
        mark("Upgrade", "UPG_COMMIT_MSG")
        tags.add("msg-upgrade")

    # 3) ENHANCEMENT rules
    for ch in changes:
        f = ch["field"]
        if f == "abis" and ch.get("added"):
            mark("Enhancement", "ENH_ABI_ADDED")
            tags.add("abi-added")
        if f == "device_profiles" and ch.get("added"):
            mark("Enhancement", "ENH_DEVICE_ADDED")
            tags.add("device-added")
        if f == "parallelism.numShards":
            try:
                if (ch["old"] or 0) < (ch["new"] or 0):
                    mark("Enhancement", "ENH_SHARDS_INCREASE")
                    tags.add("parallelism")
            except Exception:
                pass
        if f == "parallelism.matrix_axes" and ch.get("added"):
            mark("Enhancement", "ENH_MATRIX_ADDED")
            tags.add("parallelism")

    if ADD_MSG.search(commit_msg or ""):
        mark("Enhancement", "ENH_COMMIT_MSG")
        tags.add("msg-add")

    return {
        "primary_driver": primary or "Unclassified",
        "secondary_tags": sorted(list(tags)),
        "rule_ids": rule_ids,
    }

# -----------------------------
# Commands
# -----------------------------
def cmd_clone(args):
    urls = read_repo_urls(Path(args.csv))
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    for i, u in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] Cloning/fetching {u} …")
        try:
            d = ensure_cloned(u, dest)
            # (optional) sanity: count commits
            n = get_total_commits(d)
            print(f"  -> {d.name} commits={n}")
        except Exception as e:
            print(f"  !! failed: {e}")

def cmd_mine(args):
    repos_root = Path(args.repos)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    repo_dirs = [p for p in repos_root.iterdir() if (p / ".git").exists()]
    for i, repo_dir in enumerate(sorted(repo_dirs), 1):
        repo_name = repo_dir.name
        print(f"[{i}/{len(repo_dirs)}] Mining snapshots: {repo_name}")
        try:
            commits = list_relevant_commits(repo_dir)
            snapshots = []
            prev_fields = {}
            for (sha, ts, changed_paths) in commits:
                # read commit msg (for later)
                msg = sh(["git", "show", "-s", "--format=%s%n%b", sha], cwd=repo_dir).stdout.strip()
                fields = build_snapshot_for_commit(repo_dir, sha, changed_paths)
                snapshot = {
                    "repo": repo_name,
                    "sha": sha,
                    "committed_at": ts,
                    "committed_iso": datetime.utcfromtimestamp(ts).isoformat() + "Z",
                    "changed_paths": changed_paths,
                    "fields": fields,
                    "commit_msg": msg,
                }
                snapshots.append(snapshot)
                prev_fields = fields
            # write per-repo JSONL
            out_path = out_dir / f"{repo_name}.jsonl"
            safe_jsonl_write(out_path, snapshots)
            print(f"  -> wrote {out_path} ({len(snapshots)} snapshots)")
        except Exception as e:
            print(f"  !! failed: {e}")

def cmd_label(args):
    snaps_dir = Path(args.snapshots)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(snaps_dir.glob("*.jsonl"))
    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] Labeling CCEs for {f.name}")
        cces = []
        try:
            with f.open(encoding="utf-8") as fh:
                prev = None
                for line in fh:
                    snap = json.loads(line)
                    if prev is None:
                        prev = snap
                        continue
                    changes = diff_snapshots(prev["fields"], snap["fields"])
                    if changes:
                        labeling = label_commit_changes(changes, prev["fields"], snap["fields"], snap.get("commit_msg") or "")
                        cce = {
                            "repo": snap["repo"],
                            "sha": snap["sha"],
                            "committed_at": snap["committed_at"],
                            "committed_iso": snap["committed_iso"],
                            "changes": changes,                    # field-level evidence
                            "primary_driver": labeling["primary_driver"],
                            "secondary_tags": labeling["secondary_tags"],
                            "rule_ids": labeling["rule_ids"],
                            "commit_msg": snap.get("commit_msg"),
                        }
                        cces.append(cce)
                    prev = snap
            out_path = out_dir / f"{f.stem}.jsonl"
            safe_jsonl_write(out_path, cces)
            print(f"  -> wrote {out_path} ({len(cces)} CCEs)")
        except Exception as e:
            print(f"  !! failed: {e}")

def cmd_combine(args):
    import pandas as pd
    root_out = Path(args.out)
    root_out.mkdir(parents=True, exist_ok=True)
    snaps_dir = Path(args.snapshots)
    cces_dir = Path(args.cces)

    # Combine snapshots
    snaps_rows = []
    for p in sorted(snaps_dir.glob("*.jsonl")):
        with p.open(encoding="utf-8") as f:
            for line in f:
                snaps_rows.append(json.loads(line))
    df_snaps = pd.json_normalize(snaps_rows)
    snap_csv = root_out / "snapshots_master.csv"
    df_snaps.to_csv(snap_csv, index=False)
    print(f"  -> snapshots CSV: {snap_csv} ({len(df_snaps):,} rows)")
    try:
        snap_parq = root_out / "snapshots_master.parquet"
        df_snaps.to_parquet(snap_parq, index=False)
        print(f"  -> snapshots Parquet: {snap_parq}")
    except Exception:
        pass

    # Combine CCEs
    cce_rows = []
    for p in sorted(cces_dir.glob("*.jsonl")):
        with p.open(encoding="utf-8") as f:
            for line in f:
                cce_rows.append(json.loads(line))
    if cce_rows:
        df_cces = pd.json_normalize(cce_rows, sep=".")
        cce_csv = root_out / "cces_master.csv"
        df_cces.to_csv(cce_csv, index=False)
        print(f"  -> CCEs CSV: {cce_csv} ({len(df_cces):,} rows)")
        try:
            cce_parq = root_out / "cces_master.parquet"
            df_cces.to_parquet(cce_parq, index=False)
            print(f"  -> CCEs Parquet: {cce_parq}")
        except Exception:
            pass
    else:
        print("  -> No CCEs found to combine.")

# -----------------------------
# Main
# -----------------------------
def main():
    ap = argparse.ArgumentParser(description="RQ2 mining toolkit")
    sub = ap.add_subparsers()

    ap_clone = sub.add_parser("clone", help="Clone/fetch repos from CSV")
    ap_clone.add_argument("--csv", required=True, help="Path to URL_List.csv with column repo_url")
    ap_clone.add_argument("--dest", default="repos", help="Destination folder for clones")
    ap_clone.set_defaults(func=cmd_clone)

    ap_mine = sub.add_parser("mine", help="Mine snapshots (commit-level normalized fields)")
    ap_mine.add_argument("--repos", default="repos", help="Root folder of cloned repos")
    ap_mine.add_argument("--out", default="out/snapshots", help="Output folder for per-repo snapshots JSONL")
    ap_mine.set_defaults(func=cmd_mine)

    ap_label = sub.add_parser("label", help="Compute and label CCEs from snapshots")
    ap_label.add_argument("--snapshots", default="out/snapshots", help="Folder of per-repo snapshots JSONL")
    ap_label.add_argument("--out", default="out/cces", help="Output folder for per-repo CCEs JSONL")
    ap_label.set_defaults(func=cmd_label)

    ap_combine = sub.add_parser("combine", help="Combine per-repo outputs into master tables")
    ap_combine.add_argument("--snapshots", default="out/snapshots", help="Folder of snapshots JSONL")
    ap_combine.add_argument("--cces", default="out/cces", help="Folder of CCEs JSONL")
    ap_combine.add_argument("--out", default="out/master", help="Output root for combined CSV/Parquet")
    ap_combine.set_defaults(func=cmd_combine)

    args = ap.parse_args()
    if not hasattr(args, "func"):
        ap.print_help()
        sys.exit(1)
    args.func(args)

if __name__ == "__main__":
    main()

