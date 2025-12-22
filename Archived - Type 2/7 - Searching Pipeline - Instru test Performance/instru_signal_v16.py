# -*- coding: utf-8 -*-
"""
CI YAML instrumentation-testing scan (YAML + called scripts + called local action.yml).

STRICT Flutter-Android gating (UPDATED):
- A Flutter integration test (flutter test/drive with integration_test/--driver hints) is only treated as
  Android-related if EITHER:
    1) it explicitly targets Android via `-d android` / `-d emulator-*` / common Android device tokens, OR
    2) there is STRONG Android runtime evidence in the same scanned text (YAML job or called file), such as:
         - emulator actually started / waited for (emulator -avd, adb wait-for-device, android-wait-for-emulator, etc.)
         - GitHub emulator runners (reactivecircus / malinskiy / other emulator actions)
         - real-device adb serial usage
         - Android-specific 3P labs that are clearly Android (Firebase Test Lab / App Center Android / emulator.wtf)
- IMPORTANT: We DO NOT use broad "Android context" (e.g., "android sdk", "system-images;android-") as proof anymore,
  to avoid false positives where Android SDK is installed but tests are for iOS/web.

New audit columns:
    followed_files_count
    unresolved_dynamic_refs_count
    called_instru_t_ci_signal

Original output columns preserved.
"""

import re
import pandas as pd
from pathlib import Path
from typing import List, Pattern, Tuple, Dict, Any, Iterable, Optional, Set

# === CONFIG (removed for module use) ===
# This module is intended to be imported and used by a pipeline.
# Provide repo_root/full_name at call time.

# Follow-called-files knobs
FOLLOW_CALLED_FILES = True
MAX_FOLLOW_DEPTH = 2
MAX_FOLLOW_BYTES = 1_500_000  # skip very large files

# -------------------------------------------------------------------
# Shared helpers
# -------------------------------------------------------------------

def extract_full_name_from_file(filename: str) -> str:
    """owner.repo from flattened filename like owner.repo__github_actions++file.yml"""
    fname = filename.lower()
    if "__" in fname:
        return fname.split("__", 1)[0]
    return Path(fname).stem

def extract_ci_platform(filename: str) -> str:
    """ci_platform token between __ and ++ for CI YAMLs."""
    fname = filename.lower()
    if "__" in fname and "++" in fname:
        return fname.split("__", 1)[1].split("++", 1)[0]
    return ""

def compile_any(patterns: List[str], flags=re.I | re.M) -> List[Pattern]:
    return [re.compile(p, flags) for p in patterns]

def any_match(patterns: Iterable[Pattern], text: str) -> bool:
    return any(p.search(text) for p in patterns)

def unique_preserve(seq: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def collect_hits_with_groups(patterns: List[Tuple[str, str, List[Pattern]]], text: str):
    labels, groups = [], []
    for grp, lbl, pats in patterns:
        if any_match(pats, text):
            labels.append(lbl)
            groups.append(grp)
    return unique_preserve(labels), unique_preserve(groups)

# Strip shell / YAML comments (for CI scan)
COMMENT_LINE_RE = re.compile(r'(?m)^\s*(#|//|REM\b|::).*?$')
def strip_comments(raw: str) -> str:
    return COMMENT_LINE_RE.sub("", raw or "")

# -------------------------------------------------------------------
# CI YAML detector (your logic, unchanged except STRICT Flutter-Android gating)
# -------------------------------------------------------------------

def normalize_block_keys(text: str) -> str:
    text = re.sub(
        r'(?mi)^\s*(script|run|command)\s*:\s*(?!\||>|\|\-)\s*(.+)$',
        r'\2', text
    )
    text = re.sub(
        r'(?mi)^\s*(script|run|command)\s*:\s*(\||>|\|\-)\s*$', '', text
    )
    text = re.sub(
        r'(?m)^(\s*)-\s*(?=(?:\./|\.\\|bash|sh|pwsh|powershell|gradle(?:w)?|adb|flutter|gcloud|saucectl|appcenter)\b)',
        r'\1',
        text
    )
    return text

IGNORE_GHA_ACTIONS_RE = re.compile(
    r'(?mi)^\s*uses\s*:\s*('
    r'docker/(?:setup-qemu-action|setup-buildx-action|build-push-action|login-action)'
    r'|actions/checkout'
    r'|docker/setup-qemu-action'
    r'|docker/setup-buildx-action'
    r')@.*$'
)
def strip_irrelevant_ci_lines(text: str) -> str:
    return IGNORE_GHA_ACTIONS_RE.sub('', text or '')

# Gradle & shell prefixes
GRADLE_PREFIX = (
    r'^\s*'
    r'(?:\S+=\S+\s+)*'
    r'(?:sudo\s+)?'
    r'(?:(?:bash|sh)\s+-c[l]?\s+[\'"]?)?'
    r'(?:[^#\n;]*?&&\s+)?'
    r'(?:cd\s+\S+\s+&&\s+)?'
    r'(?:\./|\.\\)?gradle(?:w)?(?:\.bat)?'
)
GRADLE_ANYWHERE_RE = re.compile(r'(?i)[^\n]*\bgradle(?:w)?(?:\.bat)?[^\n]*')
GRADLE_BUILD_ACTION_RE = re.compile(
    r'(?mi)\buses\s*:\s*(gradle/gradle-build-action|gradle/actions/setup-gradle)@'
)

NON_TEST_PREFIX = (
    r'(?:assemble|bundle|package|compile|merge|process|generate|install|uninstall|jacoco|lint|publish|sign|upload)'
)
SHELL_PREFIX = (
    r'(?:\S+=\S+\s+)*(?:sudo\s+)?'
    r'(?:(?:bash|sh|pwsh|powershell)\s+-c\s+[\'"]?)?'
    r'(?:[^#\n;]*?&&\s+)?'
)

# Emulator/ADB lines
EMULATOR_LINE = rf'(?mi)^[^\n]*{SHELL_PREFIX}(?:\s|^)(?:(?:\./|\.\\)?(?:emulator)(?:\.exe)?)\b[^\n]*-avd\s+\S+'
ADB_WAIT_LINE = rf'(?mi)^[^\n]*{SHELL_PREFIX}adb\s+wait[- ]?for[- ]?device\b'
ADB_SERIAL_LINE = rf'(?mi)^[^\n]*{SHELL_PREFIX}adb\s+-s\s+(?:emulator-\d+|localhost:\d+|127\.0\.0\.1:\d+)'

# Device/Environment sources (NOT triggers)
DEVICE_SOURCES = [
    ("Real_Device", "adb -s <serial> (physical)", [
        r'(?m)^\s*adb\s+-s\s+(?!emulator-\d+\b)(?!localhost:\d+\b)(?!127\.0\.0\.1:\d+\b)\S+\b'
    ]),
    ("Emulator", "adb -s emulator-serial", [ADB_SERIAL_LINE]),
    ("Emulator", "adb wait-for-device",   [ADB_WAIT_LINE]),
    ("Emulator", "emulator -avd/@",       [EMULATOR_LINE]),
    ("Emulator", "android-wait-for-emulator", [
        r'(?m)^\s*(?:\./)?android-wait-for-emulator\b'
    ]),
    ("Emulator", "start-emulator.sh", [r'(?m)^\s*start-emulator\.sh\b']),
    ("Emulator", "android create avd", [r'\bandroid\b[^\n]*\bcreate\s+avd\b']),
    ("Emulator", "circleci android orb", [
        r"(?mi)^\s*(?:-\s*)?android/(?:start-emulator-and-run-tests|create-avd|launch-emulator)\s*:",
        r"(?mi)^\s*system-image\s*:\s*system-images;android-(?:\d+|\$[A-Z_][A-Z0-9_]*);(?:default|google_apis)[^\s]*"
    ]),
    ("Emulator", "reactivecircus runner", [
        r'(?mi)\buses\s*:\s*reactivecircus/android-emulator-runner@[\w\.\-]+'
    ]),
    ("Emulator", "malinskiy runner", [
        r'(?mi)\buses\s*:\s*malinskiy/action-android/emulator-run-cmd@[\w\.\-]+'
    ]),
    ("Emulator", "sys-img component", [
        r'(?m)^\s*-\s*sys-img-[^\s]*-android-(?:\d+|\$[A-Z_][A-Z0-9_]*)\b',
        r'(?m)^\s*-\s*sys-img-[^\s]*-google_apis-[^\s]*(?:\d+|\$[A-Z_][A-Z0-9_]*)\b'
    ]),
    ("Emulator", "avdmanager", [r'(?m)^\s*\S*avdmanager\b']),
    ("Emulator", "sdkmanager system-images/emulator", [
        r'^\s*\S*sdkmanager\b[^\n"]*"system-images;android-(?:\d+|\$[A-Z_][A-Z0-9_]*)[^"\n]*"',
        r'^\s*\S*sdkmanager\b[^\n]*\bsystem-images;android-(?:\d+|\$[A-Z_][A-Z0-9_]*)\b',
    ]),
    # Weak hints (filtered if no strong signals)
    ("Emulator", "headless flag", [
        r'(?mi)\b-no-?audio\b',
        r'(?mi)\b-no-window\b',
        r'(?mi)\b-no-boot-anim\b'
    ]),
    ("Emulator", "avd-name", [
        r'(?mi)^\s*avd[-_ ]?name\s*:\s*\S+'
    ]),
    ("Emulator", "api-level", [
        r'(?mi)\bapi[-_ ]?level\s*:\s*\d{2,}|\bapi_level\s*:\s*\d{2,}'
    ]),
    ("Emulator", "abi/arch", [
        r'\b(abi|arch)\b\s*:?\s*(x86|x86_64|arm64|armeabi)'
    ]),
    ("Emulator", "target image", [
        r'\btarget\s*:\s*(google_apis|google_apis_playstore|aosp.*)'
    ]),
    ("Emulator", "device name", [
        r'\b(avd[-_ ]?name|device)\b\s*:\s*pixel'
    ]),
    # Third-party labs (environment)
    ("Third_Party_Lab", "gcloud firebase", [
        r'(?mi)^[^\n]*\bgcloud(?:\s+beta)?\s+firebase\s+test\s+android\s+run\b'
    ]),
    ("Third_Party_Lab", "saucectl", [
        r'(?mi)^[^\n]*\bsaucectl(?:\s+run)?\b'
    ]),
    ("Third_Party_Lab", "browserstack/bstack", [
        r'(?i)\b(browserstack|bstack)\b'
    ]),
    ("Third_Party_Lab", "appcenter test", [
        r'(?mi)^[^\n]*\bappcenter\s+test\s+run\s+android\b'
    ]),
    ("Third_Party_Lab", "maestro cloud", [
        r'(?mi)^[^\n]*\bmaestro\s+cloud\b'
    ]),
    ("Third_Party_Lab", "emulator.wtf action", [
        r'(?mi)^\s*uses\s*:\s*emulator-wtf/run-tests@[\w\.\-]+',
        r'(?i)\bemulator\.wtf\b'
    ]),
    # Other emulator actions
    ("Emulator", "other gha emulator", [
        r'(?mi)^\s*uses\s*:\s*vgaidarji/android-github-actions-emulator@[\w\.\-]+',
        r'(?mi)^\s*uses\s*:\s*(?!reactivecircus/android-emulator-runner@)'
        r'(?!malinskiy/action-android/emulator-run-cmd@)'
        r'(?!emulator-wtf/run-tests@)'
        r'[\w\.-]+/[\w\./-]*android[\w\./-]*(?:\bemulator\b|\bavd\b)[\w\./-]*@[\w\.\-]+'
    ]),
]

DEVICE_PATTERNS = [(grp, lbl, compile_any(pats)) for (grp, lbl, pats) in DEVICE_SOURCES]

# Sanitizers
EXCLUDED_TASK_SEGMENT_RE = re.compile(
    r'(^|\s)(?:-x|--exclude-task)\s+(["\']?)[:\w\.-]*(?:androidtest|baselineprofile)[\w:\.-]*\2\b',
    re.IGNORECASE | re.MULTILINE,
)
GHA_EXPR_RE = re.compile(r"\${{\s*[^}]+}}")

def remove_excluded_gradle_tasks(text: str) -> str:
    return EXCLUDED_TASK_SEGMENT_RE.sub(lambda m: (m.group(1) or " "), text or "")

def pre_sanitize(text: str) -> str:
    t = remove_excluded_gradle_tasks(text)
    return GHA_EXPR_RE.sub("", t or "")

# Triggers (primary + anywhere) incl. BaselineProfile & connectedBenchmarkAndroidTest
TRIGGER_SOURCES_PRIMARY = [
    ("Gradle",  "connectedAndroidTest",
     [rf'(?mi){GRADLE_PREFIX}[^\n\r]*\b(?:[:\w-]+:)*connectedandroidtest\b[^\n\r]*']),
    ("Gradle", "connected.*Android.*", [
        rf'''(?mix){GRADLE_PREFIX}[^\n\r]*\b(?:[:\w-]+:)*connected[a-z0-9:_-]*android[a-z0-9:_-]*test\b(?![^\n\r]*\b(?:{NON_TEST_PREFIX})[\w-]*androidtest\b)[^\n\r]*'''
    ]),
    ("Gradle",  "connectedCheck",
     [rf'(?mi){GRADLE_PREFIX}[^\n\r]*\b(?:[:\w-]+:)*connectedcheck\b[^\n\r]*']),
    ("Gradle",  "cAT shorthand",
     [rf'(?mi){GRADLE_PREFIX}[^\n\r]*\b(?:[:\w-]+:)*cAT\b[^\n\r]*']),
    ("Gradle",  "deviceCheck",
     [rf'(?mi){GRADLE_PREFIX}[^\n\r]*\b(?:[:\w-]+:)*(?:devicecheck|alldevicechecks)\b[^\n\r]*']),
    ("Gradle",  "managedDevice AndroidTest",
     [rf'(?mi){GRADLE_PREFIX}[^\n\r]*\b(?!{NON_TEST_PREFIX})(?:manageddevice|device)[\w:-]*androidtest\b[^\n\r]*']),
    ("Gradle", "variant/device AndroidTest",
     [rf'''(?mix){GRADLE_PREFIX}[^\n\r]*\b(?:(?:[:\w-]+:)*(?!{NON_TEST_PREFIX})(?!connected)(?!spoon)(?!marathon)[A-Za-z0-9][\w-]*androidtest\b)[^\n\r]*''']),
    ("Gradle",  "Spoon",    [rf'(?mi){GRADLE_PREFIX}[^\n\r]*\bspoon(?:\w*androidtest)?\b']),
    ("Gradle",  "Marathon", [rf'(?mi){GRADLE_PREFIX}[^\n\r]*\bmarathon(?:\w*androidtest)?\b']),
    ("ADB",     "am instrument", [r'(?mi)^[^\n]*\bam\s+instrument\b']),
    # 3P CLIs (instr only)
    ("Third_Party_Lab", "gcloud firebase (instr)", [
        r'(?mi)\bgcloud(?:\s+beta)?\s+firebase\s+test\s+android\s+run[^\n]*\b(--test\b|--type\s+instrumentation\b)'
    ]),
    ("Third_Party_Lab", "flank",            [r'(?mi)^[^\n]*\bflank\s+android\s+run\b']),
    ("Third_Party_Lab", "saucectl",         [r'(?mi)^[^\n]*\bsaucectl(?:\s+run)?\b']),
    ("Third_Party_Lab", "appcenter run",    [r'(?mi)^[^\n]*\bappcenter\s+test\s+run\s+android\b']),
    ("Third_Party_Lab", "emulator.wtf run", [
        r'(?mi)^\s*uses\s*:\s*emulator-wtf/run-tests@[\w\.\-]+',
        r'(?i)\bemulator\.wtf\b'
    ]),
    # BaselineProfile + connectedBenchmarkAndroidTest
    ("Gradle", "generateBaselineProfile", [
        rf'(?mi){GRADLE_PREFIX}[^\n\r]*\b(?:[:\w-]+:)*generate(?:\w*?)baselineprofile\b[^\n\r]*'
    ]),
    ("Gradle", "collectBaselineProfile", [
        rf'(?mi){GRADLE_PREFIX}[^\n\r]*\b(?:[:\w-]+:)*collect(?:\w*?)baselineprofile\b[^\n\r]*'
    ]),
    ("Gradle", "connectedBenchmarkAndroidTest", [
        rf'(?mi){GRADLE_PREFIX}[^\n\r]*\b(?:[:\w-]+:)*connectedbenchmarkandroidtest\b[^\n\r]*'
    ]),
]

TRIGGER_SOURCES_ANYWHERE = [
    ("Gradle", "connected (anywhere)", [
        r'(?mix)[^\n]*\bgradle(?:w)?(?:\.bat)?[^\n]*\b(?:[:\w-]+:)*connected[a-z0-9:_-]*android[a-z0-9:_-]*test\b'
    ]),
    ("Gradle", "connectedAndroidTest (anywhere)", [
        r'(?mi)[^\n]*\bgradle(?:w)?(?:\.bat)?[^\n]*\bconnectedandroidtest\b'
    ]),
    ("Gradle", "connectedCheck (anywhere)", [
        r'(?mi)[^\n]*\bgradle(?:w)?(?:\.bat)?[^\n]*\b(?:[:\w-]+:)*connectedcheck\b'
    ]),
    ("Gradle", "managedDevice AndroidTest (anywhere)", [
        rf'(?mix)[^\n]*\bgradle(?:w)?(?:\.bat)?[^\n]*\b(?:(?:[:\w-]+:)*(?!{NON_TEST_PREFIX})(?!connected)(?!spoon)(?!marathon)[A-Za-z0-9][\w-]*androidtest\b)'
    ]),
    ("Gradle", "variant/device AndroidTest (anywhere)", [
        rf'(?mix)[^\n]*\bgradle(?:w)?(?:\.bat)?[^\n]*\b(?:(?:[:\w-]+:)*(?!{NON_TEST_PREFIX})[\w-]*androidtest\b)'
    ]),
    ("Gradle", "Spoon (anywhere)", [
        r'(?mi)[^\n]*\bgradle(?:w)?(?:\.bat)?[^\n]*\bspoon(?:\w*androidtest)?\b'
    ]),
    ("Gradle", "Marathon (anywhere)", [
        r'(?mi)[^\n]*\bgradle(?:w)?(?:\.bat)?[^\n]*\bmarathon(?:\w*androidtest)?\b'
    ]),
    # BaselineProfile + connectedBenchmarkAndroidTest
    ("Gradle", "generateBaselineProfile (anywhere)", [
        rf'(?mi)[^\n]*\bgradle(?:w)?(?:\.bat)?[^\n]*\bgenerate(?:\w*?)baselineprofile\b'
    ]),
    ("Gradle", "collectBaselineProfile (anywhere)", [
        rf'(?mi)[^\n]*\bgradle(?:w)?(?:\.bat)?[^\n]*\bcollect(?:\w*?)baselineprofile\b'
    ]),
    ("Gradle", "connectedBenchmarkAndroidTest (anywhere)", [
        rf'(?mi)[^\n]*\bgradle(?:w)?(?:\.bat)?[^\n]*\bconnectedbenchmarkandroidtest\b'
    ]),
]

TRIGGER_PATTERNS_PRIMARY  = [(grp, lbl, compile_any(pats)) for (grp, lbl, pats) in TRIGGER_SOURCES_PRIMARY]
TRIGGER_PATTERNS_ANYWHERE = [(grp, lbl, compile_any(pats)) for (grp, lbl, pats) in TRIGGER_SOURCES_ANYWHERE]

# Gradle action inputs (GHA)
GHA_GRADLE_INPUTS = compile_any([
    rf'''(?mix)^\s*(arguments|tasks|script|cmd)\s*:\s*[^$`\n]*\b(?:[:\w-]+:)*connected(?:\${{\s*[^}}]+\s*}}|[^\n\r])*?android(?:\${{\s*[^}}]+\s*}}|[^\n\r])*?test\b''',
    r'(?mi)^\s*(arguments|tasks|script|cmd)\s*:\s*[^$`\n]*\b(?:[:\w-]+:)*connectedcheck\b',
    r'(?mi)^\s*(arguments|tasks|script|cmd)\s*:\s*[^$`\n]*\b(?:[:\w-]+:)*(?:devicecheck|alldevicechecks)\b',
    rf'''(?mix)^\s*(arguments|tasks|script|cmd)\s*:\s*[^$`\n]*\b(?:(?:[:\w-]+:)*(?!{NON_TEST_PREFIX})[\w-]*androidtest\b)''',
    r'(?mi)^\s*(arguments|tasks|script|cmd)\s*:\s*[^$`\n]*\b(?:[:\w-]+:)*cAT\b',
])

GHA_GMD_INPUTS = compile_any([
    rf'(?mi)^\s*(arguments|tasks|script|cmd)\s*:\s*[^$`\n]*\b(?!(?:[:\w-]+:)*(?:connected[a-z0-9:._-]*|{NON_TEST_PREFIX})[\w:-]*androidtest\b)(?:[:\w-]+:)*[\w:-]*androidtest\b'
])

# Flutter integration test detectors
FLUTTER_IT_LINE = re.compile(r'(?mi)^\s*flutter\s+(?:test|drive)\b[^\n]*')
FLUTTER_IT_ANDROID_HINT = re.compile(r'(?i)(integration_test|--driver\b|/integration_test/)')
FLUTTER_DEVICE_FLAG_RE = re.compile(r'(?i)\s+-d\s+(?P<dev>"[^"]+"|\'[^\']+\'|\S+)')
FLUTTER_DEVICE_IS_ANDROID = re.compile(
    r'(?i)\b(android|emulator-\d+|sdk\s+gphone|android\s+sdk\s+built\s+for|pixel)\b'
)
LINUX_HEADLESS_HINTS_RE = re.compile(r'(?mi)^\s*(xvfb-run|export\s+DISPLAY=|sudo\s+Xvfb)\b')

# Provider/env helpers
INLINE_DEVICE_HINTS = compile_any([
    r'(?mi)^\s*devices\s*:\s*\|',
    r'(?mi)\b--device\b',
    r'(?mi)\bmodel\s*=\s*[^,\s]+',
    r'(?mi)\bversion\s*=\s*\d+',
    r'(?mi)\blocale\s*=\s*[-\w]+',
    r'(?mi)\borientation\s*=\s*(portrait|landscape)',
    r'(?mi)^\s*with-orchestrator\s*:\s*true\b',
    r'(?mi)\b--use-orchestrator\b',
    r'(?mi)\bnum-flaky-test-attempts\s*:\s*\d+\b',
    r'(?mi)\b--num-flaky-test-attempts(?:=|\s+)\d+\b',
])

CONFIG_FILE_HINTS = compile_any([
    r'(?mi)\.ewtf\.ya?ml\b',
    r'(?mi)\b(flank\.ya?ml|flank\.android\.ya?ml)\b',
    r'(?mi)\b--config(?:=|\s+)\S+',
    r'(?mi)\b(browserstack\.ya?ml)\b',
    r'(?mi)\b(bs(?:config)?\.ya?ml)\b',
])

def detect_inline_env(text: str) -> bool:
    return any_match(INLINE_DEVICE_HINTS, text)

def detect_config_env(text: str) -> bool:
    return any_match(CONFIG_FILE_HINTS, text)

# Strong/weak classification
EMULATOR_STRONG_LABELS = {
    "emulator -avd/@", "android-wait-for-emulator", "start-emulator.sh",
    "circle-android wait-for-boot", "reactivecircus runner", "avdmanager",
    "sdkmanager system-images/emulator", "adb -s emulator-serial", "adb wait-for-device",
    "android create avd", "malinskiy runner", "other gha emulator", "circleci android orb",
}
THIRD_PARTY_STRONG_LABELS = {
    "gcloud firebase", "emulator.wtf action", "saucectl",
    "browserstack/bstack", "appcenter test", "maestro cloud"
}
REAL_DEVICE_STRONG_LABELS = {"adb -s <serial> (physical)"}
REAL_DEVICE_GENERIC_ADB = set()

STRONG_DEVICE_LABELS = (
    EMULATOR_STRONG_LABELS |
    REAL_DEVICE_STRONG_LABELS |
    THIRD_PARTY_STRONG_LABELS
)

def filter_weak_device_hints(labels, groups):
    lbl_set = set(labels)
    if not (lbl_set & STRONG_DEVICE_LABELS):
        labels = [l for l in labels if l in STRONG_DEVICE_LABELS]
        if not labels:
            groups = []
    return labels, groups

def reconcile_emulator_vs_real(labels, groups):
    lbls = set(labels)
    if lbls & EMULATOR_STRONG_LABELS:
        lbls -= REAL_DEVICE_GENERIC_ADB
        labels = [l for l in labels if l in lbls]
        if "Real_Device" in groups:
            has_real_after = bool(set(labels) & REAL_DEVICE_STRONG_LABELS)
            if not has_real_after:
                groups = [g for g in groups if g != "Real_Device"]
    return labels, groups

ANDROID_CONTEXT_RE = re.compile(
    r'(?i)\b(adb|avd|emulator|android\s+sdk|system-images;android-|androidtest|connected(check|androidtest)|gcloud\s+firebase\s+test\s+android\s+run)\b'
)

# -------------------------------------------------------------------
# STRICT Flutter-Android runtime evidence (NEW)
# -------------------------------------------------------------------

# Only these labels count as "strong runtime Android evidence" for Flutter gating
# (NOTE: intentionally excludes generic setup-only hints like api-level, sdkmanager install, sys-img component, etc.)
ANDROID_RUNTIME_ENV_LABELS = {
    # emulator runtime / wait / runners
    "emulator -avd/@",
    "adb wait-for-device",
    "adb -s emulator-serial",
    "android-wait-for-emulator",
    "start-emulator.sh",
    "reactivecircus runner",
    "malinskiy runner",
    "other gha emulator",
    "circleci android orb",
    # real device
    "adb -s <serial> (physical)",
}

# 3P labels that are clearly Android for this study
ANDROID_STRICT_3P_LABELS = {
    "gcloud firebase",       # firebase test android run
    "appcenter test",        # appcenter test run android
    "emulator.wtf action",   # android emulator service
}

_ANDROID_RUNTIME_ENV_LABELS_L = {s.lower() for s in ANDROID_RUNTIME_ENV_LABELS}
_ANDROID_STRICT_3P_LABELS_L   = {s.lower() for s in ANDROID_STRICT_3P_LABELS}

def has_android_runtime_evidence(dev_labels: List[str], dev_groups: List[str]) -> bool:
    """
    STRICT proof that the Flutter integration test is related to Android runtime.
    We do NOT accept broad Android SDK context as proof.
    """
    lbls = {str(l).strip().lower() for l in (dev_labels or []) if str(l).strip()}
    grps = {str(g).strip() for g in (dev_groups or []) if str(g).strip()}

    # real device group is runtime by definition (from adb -s physical patterns)
    if "Real_Device" in grps:
        return True

    # emulator/runtime labels
    if lbls & _ANDROID_RUNTIME_ENV_LABELS_L:
        return True

    # strict Android third-party
    if lbls & _ANDROID_STRICT_3P_LABELS_L:
        return True

    return False

# -------------------------------------------------------------------
# Job splitter for CI YAML
# -------------------------------------------------------------------

JOBS_ANCHOR_RE = re.compile(r'(?m)^(?P<indent>\s*)jobs\s*:\s*$')
ANY_KEY_RE     = re.compile(r'(?m)^(?P<indent>\s*)(?P<name>[\w-]+)\s*:\s*$')

def split_jobs_blocks(raw: str) -> List[Tuple[str, str]]:
    m = JOBS_ANCHOR_RE.search(raw)
    if not m:
        return [("__whole__", raw)]
    jobs_indent = len(m.group("indent"))
    lines = raw.splitlines(True)
    start_idx = raw[:m.end()].count("\n")
    candidates = []
    for i in range(start_idx, len(lines)):
        lm = ANY_KEY_RE.match(lines[i])
        if not lm:
            continue
        indent = len(lm.group("indent"))
        if indent > jobs_indent:
            candidates.append((i, indent, lm.group("name")))
    if not candidates:
        return [("__whole__", raw)]
    min_indent = min(indent for _, indent, _ in candidates)
    job_headers = [(i, name) for (i, indent, name) in candidates if indent == min_indent]
    if not job_headers:
        return [("__whole__", raw)]
    blocks = []
    header_indices = [i for i, _ in job_headers] + [len(lines)]
    for idx in range(len(job_headers)):
        i, name = job_headers[idx]
        j = header_indices[idx + 1]
        block_text = "".join(lines[i:j])
        blocks.append((name, block_text))
    return blocks

# -------------------------------------------------------------------
# Invocation/env mapping helpers
# -------------------------------------------------------------------

def has_gmd_gradle_trigger(trigger_labels: List[str]) -> bool:
    L = {l.lower() for l in trigger_labels}
    if any("manageddevice androidtest" in l for l in L):
        return True
    if ("variant/device androidtest" in L and not any(
        x in L for x in {
            "connected.*android.*",
            "connectedandroidtest",
            "connectedbenchmarkandroidtest",
            "spoon",
            "marathon"
        }
    )):
        return True
    return False

def has_connected_gradle_trigger(trigger_labels: List[str]) -> bool:
    L = {l.lower() for l in trigger_labels}
    keys = {
        "connected.*android.*",
        "connectedandroidtest",
        "connectedbenchmarkandroidtest",
        "connectedcheck",
        "cat shorthand",
        "connected (anywhere)",
        "connectedandroidtest (anywhere)",
        "connectedcheck (anywhere)",
        "spoon",
        "marathon",
        "devicecheck",
        "gha gradle inputs/script",
    }
    return any(k in L for k in keys) or any(
        ("connected" in l and "android" in l and "test" in l) for l in L
    )

def has_baselineprofile_trigger(trigger_labels: List[str]) -> bool:
    L = {l.lower() for l in trigger_labels}
    return any("baselineprofile" in l for l in L)

def map_test_invocations(
    groups: List[str],
    trigger_labels: List[str],
    flutter_it_androidish: bool
) -> List[str]:
    s_groups = set(groups)
    L = {l.lower() for l in trigger_labels}
    out: List[str] = []
    if has_gmd_gradle_trigger(trigger_labels):
        out.append("Gradle_GMD")
    if has_connected_gradle_trigger(trigger_labels):
        out.append("Gradle_Connected")
    if has_baselineprofile_trigger(trigger_labels):
        out.append("Gradle")
    if "ADB" in s_groups:
        out.append("ADB")
    if "Third_Party_Lab" in s_groups:
        out.append("3P CLIs")

    has_gradle_or_adb = any(x in out for x in ["Gradle_GMD", "Gradle_Connected", "Gradle", "ADB"])
    if (not has_gradle_or_adb) and ("flutter integration test" in L) and flutter_it_androidish:
        out.append("3P CLIs")

    return sorted(set(out))

def map_execution_envs(groups: List[str], labels: List[str]) -> List[str]:
    envs = set()
    s_groups, s_labels = set(groups), set(labels)
    if "Third_Party_Lab" in s_groups:
        envs.add("Third Party")
    if "Real_Device" in s_groups:
        envs.add("Real Device")
    if "Emulator" in s_groups:
        if "reactivecircus runner" in s_labels:
            envs.add("Emulator_ReactiveCircus")
        elif "malinskiy runner" in s_labels:
            envs.add("Emulator_Malinskiy")
        elif ("other gha emulator" in s_labels) or ("circleci android orb" in s_labels):
            envs.add("Emulator_Other")
        else:
            envs.add("Emulator_DIY")
    return sorted(envs)

def map_exec_env_style(exec_envs: List[str]) -> str:
    styles = set()

    # Emulator style mapping (UPDATED):
    # - DIY emulator setup => Emu_Custom
    # - All emulator actions/runners (ReactiveCircus, Malinskiy, Other) => Emu_Community
    if any(e.startswith("Emulator_") for e in exec_envs):
        if "Emulator_DIY" in exec_envs:
            styles.add("Emu_Custom")

        if any(e in {"Emulator_ReactiveCircus", "Emulator_Malinskiy", "Emulator_Other"} for e in exec_envs):
            styles.add("Emu_Community")

    if "Third Party" in exec_envs:
        styles.add("Third-Party")
    if "Real Device" in exec_envs:
        styles.add("Real Device")
    if "GMD" in exec_envs:
        styles.add("GMD")

    return ",".join(sorted(styles)) if styles else ""


def map_test_inv_style(test_inv: str) -> str:
    tokens = {t.strip() for t in test_inv.split(",") if t.strip()}
    styles = set()
    if any(t.startswith("Gradle") for t in tokens):
        styles.add("Gradle-based")
    if "3P CLIs" in tokens:
        styles.add("Third-Party CLI")
    if "ADB" in tokens:
        styles.add("ADB")
    return ",".join(sorted(styles)) if styles else ""

# -------------------------------------------------------------------
# Called-file following (scripts + local action.yml)
# -------------------------------------------------------------------

LOCAL_USES_RE = re.compile(r'(?mi)^\s*uses\s*:\s*(?P<ref>\./\S+?)(?:\s+#.*)?$')
WORKDIR_RE = re.compile(r'(?mi)^\s*working-directory\s*:\s*(?P<wd>[^\n#]+)')

SCRIPT_CALL_RE = re.compile(r'''(?mix)
(?:^|[;&|()\s"'`])
(?:(?:bash|sh|pwsh|powershell|python|python3|node|ruby)\s+)?
(?P<path>(?:\./|\.\\)?[\w./\\-]+\.(?:sh|ps1|bat|cmd|py|js|rb|pl))
(?:\s|$)
''')

GENERIC_REL_EXEC_RE = re.compile(r'(?m)(?:^|[;&|()\s"\'`])(?P<path>\./[A-Za-z0-9_./\\-]+)(?:\s|$)')
CONFIG_ARG_RE = re.compile(r'(?mi)\b--config(?:=|\s+)(?P<path>[^\s"\']+)')

NO_FOLLOW_BASENAMES = {"gradlew", "gradlew.bat", "gradle", "adb", "flutter"}

def _strip_quotes(s: str) -> str:
    return (s or "").strip().strip('"').strip("'").strip("`")

def is_dynamic_ref(ref: str) -> bool:
    r = ref or ""
    return ("${{" in r) or ("${" in r) or ("$(" in r) or ("%{" in r)

def extract_workdirs(text: str) -> List[str]:
    wds = []
    for m in WORKDIR_RE.finditer(text or ""):
        wd = _strip_quotes(m.group("wd"))
        if wd:
            wd = wd.replace("\\", "/").lstrip("./")
            wds.append(wd)
    return unique_preserve(wds)

def extract_references(text: str) -> List[str]:
    refs: List[str] = []

    for m in LOCAL_USES_RE.finditer(text or ""):
        ref = _strip_quotes(m.group("ref"))
        if "@" in ref:
            ref = ref.split("@", 1)[0]
        refs.append(ref)

    for m in SCRIPT_CALL_RE.finditer(text or ""):
        refs.append(_strip_quotes(m.group("path")))

    for m in CONFIG_ARG_RE.finditer(text or ""):
        refs.append(_strip_quotes(m.group("path")))

    for m in GENERIC_REL_EXEC_RE.finditer(text or ""):
        p = _strip_quotes(m.group("path"))
        base = Path(p.replace("\\", "/")).name.lower()
        if base in NO_FOLLOW_BASENAMES:
            continue
        refs.append(p)

    out = []
    for r in refs:
        if not r:
            continue
        out.append(r.replace("\\", "/").strip())
    return unique_preserve(out)

def safe_read_text(p: Path) -> str:
    try:
        if p.is_file() and p.stat().st_size > MAX_FOLLOW_BYTES:
            return ""
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

def repo_roots_for_repo(repo_root: Path) -> List[Path]:
    """Return roots used to resolve called scripts/actions.

    In the redesigned pipeline, called files live inside the local clone.
    """
    return [repo_root]

def _try_with_extensions(p: Path) -> List[Path]:
    if p.exists():
        return [p]
    if p.suffix:
        return []
    exts = [".sh", ".ps1", ".bat", ".cmd", ".py", ".js", ".rb", ".pl", ".yml", ".yaml"]
    out = []
    for e in exts:
        cand = p.with_suffix(e)
        if cand.exists():
            out.append(cand)
    return out

def resolve_ref(repo_root: Path, ref: str, workdirs: Optional[List[str]] = None) -> List[Path]:
    if not ref:
        return []
    if is_dynamic_ref(ref):
        return []

    ref = _strip_quotes(ref.replace("\\", "/").strip())

    ref_norm = ref
    if ref_norm.startswith("./"):
        ref_norm = ref_norm[2:]
    ref_norm = ref_norm.lstrip("/")

    wds = workdirs or []
    prefixes = [""] + [wd.strip("/").replace("\\", "/") for wd in wds if wd]
    roots = repo_roots_for_repo(repo_root)
    out: List[Path] = []

    for root in roots:
        for pref in prefixes:
            candidate = root / pref / ref_norm if pref else (root / ref_norm)

            if candidate.is_dir():
                for nm in ("action.yml", "action.yaml"):
                    cand = candidate / nm
                    if cand.exists():
                        out.append(cand)
                continue

            if candidate.exists():
                out.append(candidate)
                continue

            out.extend(_try_with_extensions(candidate))

    # dedupe
    seen: Set[str] = set()
    uniq: List[Path] = []
    for p in out:
        key = str(p.resolve()) if p.exists() else str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq

def scan_text_for_signals(text: str) -> Dict[str, Any]:
    content = strip_comments(text)
    content = normalize_block_keys(content)
    content = strip_irrelevant_ci_lines(content)
    content_for_triggers = pre_sanitize(content)
    low = (content_for_triggers or "").lower()

    env_declared = False
    env_location = "none"
    if detect_inline_env(content_for_triggers):
        env_declared = True
        env_location = "inline"
    elif detect_config_env(content_for_triggers):
        env_declared = True
        env_location = "config"

    dev_labels, dev_groups = collect_hits_with_groups(DEVICE_PATTERNS, low)
    dev_labels, dev_groups = filter_weak_device_hints(dev_labels, dev_groups)
    dev_labels, dev_groups = reconcile_emulator_vs_real(dev_labels, dev_groups)

    trig_labels, trig_groups = collect_hits_with_groups(TRIGGER_PATTERNS_PRIMARY, low)
    fb_trig_labels, fb_trig_groups = collect_hits_with_groups(TRIGGER_PATTERNS_ANYWHERE, low)
    if fb_trig_labels:
        trig_labels = unique_preserve(trig_labels + fb_trig_labels)
        trig_groups = unique_preserve(trig_groups + fb_trig_groups)

    flutter_devices: List[str] = []
    flutter_it_present = False
    flutter_it_androidish = False
    flutter_it_android_targeted = False

    flutter_it_hits = []
    for m in FLUTTER_IT_LINE.finditer(content_for_triggers):
        line = m.group(0)
        if FLUTTER_IT_ANDROID_HINT.search(line) or FLUTTER_DEVICE_IS_ANDROID.search(line):
            flutter_it_hits.append(line)

    if flutter_it_hits:
        flutter_it_present = True
        trig_labels = unique_preserve(trig_labels + ["flutter integration test"])
        trig_groups = unique_preserve(trig_groups + ["Flutter"])

        for line in flutter_it_hits:
            d = FLUTTER_DEVICE_FLAG_RE.search(line)
            if d:
                plat = d.group("dev").strip('"\'').lower()
                if (
                    plat == "android" or
                    plat.startswith("emulator-") or
                    "sdk gphone" in plat or
                    "android sdk built for" in plat or
                    "pixel" in plat
                ):
                    flutter_it_android_targeted = True
                    if "android" not in flutter_devices:
                        flutter_devices.append("android")
                elif plat in {"linux", "macos", "windows"}:
                    if plat not in flutter_devices:
                        flutter_devices.append(plat)
                elif plat in {"ios", "iphone", "ipad", "iphone simulator"}:
                    if "ios" not in flutter_devices:
                        flutter_devices.append("ios")
                elif plat in {"web", "web-server", "chrome", "edge", "firefox", "safari"}:
                    if "web" not in flutter_devices:
                        flutter_devices.append("web")

        if (not flutter_devices) and LINUX_HEADLESS_HINTS_RE.search(content_for_triggers):
            if "linux" not in flutter_devices:
                flutter_devices.append("linux")

    # STRICT: do NOT use broad ANDROID_CONTEXT_RE here.
    has_android_env_strict = has_android_runtime_evidence(dev_labels, dev_groups)
    if flutter_it_hits and (flutter_it_android_targeted or has_android_env_strict):
        flutter_it_androidish = True

    return {
        "trigger_labels": trig_labels,
        "trigger_groups": trig_groups,
        "device_labels": dev_labels,
        "device_groups": dev_groups,
        "flutter_it_present": flutter_it_present,
        "flutter_devices": flutter_devices,
        "flutter_it_androidish": flutter_it_androidish,
        "env_declared": env_declared,
        "env_location": env_location,
    }

# cache for followed files
_FOLLOW_SCAN_CACHE: Dict[str, Dict[str, Any]] = {}
_FOLLOW_REFS_CACHE: Dict[str, List[str]] = {}
_FOLLOW_WD_CACHE: Dict[str, List[str]] = {}

def follow_and_scan_called_files(
    repo_root: Path,
    initial_refs: List[str],
    initial_workdirs: List[str],
    max_depth: int = MAX_FOLLOW_DEPTH,
) -> Dict[str, Any]:
    agg = {
        "trigger_labels": [],
        "trigger_groups": [],
        "device_labels": [],
        "device_groups": [],
        "flutter_it_present": False,
        "flutter_it_androidish": False,
        "flutter_devices": [],
        "env_declared": False,
        "env_location": "none",
        "scanned_files": [],
        "unresolved_dynamic_refs_count": 0,
    }

    visited: Set[str] = set()

    def merge_scan(scan: Dict[str, Any]) -> None:
        agg["trigger_labels"] = unique_preserve(agg["trigger_labels"] + scan.get("trigger_labels", []))
        agg["trigger_groups"] = unique_preserve(agg["trigger_groups"] + scan.get("trigger_groups", []))
        agg["device_labels"]  = unique_preserve(agg["device_labels"]  + scan.get("device_labels", []))
        agg["device_groups"]  = unique_preserve(agg["device_groups"]  + scan.get("device_groups", []))

        if scan.get("flutter_it_present"):
            agg["flutter_it_present"] = True
        if scan.get("flutter_it_androidish"):
            agg["flutter_it_androidish"] = True
        agg["flutter_devices"] = unique_preserve(agg["flutter_devices"] + scan.get("flutter_devices", []))

        if scan.get("env_declared"):
            if (not agg["env_declared"]) or (agg["env_location"] != "inline"):
                agg["env_declared"] = True
                agg["env_location"] = scan.get("env_location", "config") or "config"

    def walk_file(p: Path, depth: int) -> None:
        if depth > max_depth:
            return

        try:
            key = str(p.resolve())
        except Exception:
            key = str(p)

        if key in visited:
            return
        visited.add(key)

        agg["scanned_files"].append(key)

        if key in _FOLLOW_SCAN_CACHE:
            scan = _FOLLOW_SCAN_CACHE[key]
        else:
            txt = safe_read_text(p)
            scan = scan_text_for_signals(txt)
            _FOLLOW_SCAN_CACHE[key] = scan

            if key not in _FOLLOW_REFS_CACHE:
                _FOLLOW_REFS_CACHE[key] = extract_references(txt)
            if key not in _FOLLOW_WD_CACHE:
                _FOLLOW_WD_CACHE[key] = extract_workdirs(txt)

        merge_scan(scan)

        if depth == max_depth:
            return

        child_refs = _FOLLOW_REFS_CACHE.get(key, [])
        child_wds  = _FOLLOW_WD_CACHE.get(key, [])

        for r in child_refs:
            if is_dynamic_ref(r):
                agg["unresolved_dynamic_refs_count"] += 1
                continue
            for child_path in resolve_ref(repo_root, r, child_wds):
                walk_file(child_path, depth + 1)

    for r in unique_preserve(initial_refs):
        if is_dynamic_ref(r):
            agg["unresolved_dynamic_refs_count"] += 1
            continue
        for p in resolve_ref(repo_root, r, initial_workdirs):
            walk_file(p, depth=0)

    agg["scanned_files"] = unique_preserve(agg["scanned_files"])
    return agg

def called_instru_signal_from_evidence(ev: Dict[str, Any]) -> bool:
    trig = ev.get("trigger_labels", []) or []
    non_flutter = [l for l in trig if l.lower() != "flutter integration test"]

    has_device_any = any(
        g in {"Emulator", "Third_Party_Lab", "Real_Device"}
        for g in (ev.get("device_groups", []) or [])
    )
    # STRICT: Flutter counts only if androidish is True (now strict)
    flutter_counts = bool(ev.get("flutter_it_present")) and bool(ev.get("flutter_it_androidish"))

    return bool(non_flutter or has_device_any or flutter_counts)

# -------------------------------------------------------------------
# Per-file analyzer (YAML)
# -------------------------------------------------------------------

def analyze_ci_yaml_file(path: Path, *, full_name: str, repo_root: Path, ci_platform: str = "GitHub_Actions") -> Dict[str, Any]:
    """Analyze a single GitHub Actions workflow YAML file.

    Args:
        path: Path to the workflow YAML inside the repo clone.
        full_name: 'owner/repo' identifier.
        repo_root: Local clone root.
        ci_platform: Kept for compatibility; defaults to 'GitHub_Actions'.
    """
    filename = path.name

    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        raw = ""

    file_trigger_labels: List[str] = []
    file_device_labels:  List[str] = []
    file_trigger_groups: List[str] = []
    file_device_groups:  List[str] = []
    file_instru_signal_any = False
    file_flutter_devices: List[str] = []

    file_flutter_it_present = False
    file_flutter_it_androidish = False

    file_env_declared = False
    file_env_location = "none"

    # audit
    followed_files_count = 0
    unresolved_dynamic_refs_count = 0
    called_instru_t_ci_signal = False

    refs_from_yaml: List[str] = []
    workdirs_from_yaml: List[str] = []

    for job_name, job_raw in split_jobs_blocks(raw):
        refs_from_yaml += extract_references(job_raw)
        workdirs_from_yaml += extract_workdirs(job_raw)

        content = strip_comments(job_raw)
        content = normalize_block_keys(content)
        content = strip_irrelevant_ci_lines(content)
        content_for_triggers = pre_sanitize(content)

        inline_decl = detect_inline_env(content_for_triggers)
        config_decl = detect_config_env(content_for_triggers)
        if inline_decl:
            file_env_declared = True
            file_env_location = "inline"
        elif (not file_env_declared) and config_decl:
            file_env_declared = True
            file_env_location = "config"

        dev_labels, dev_groups = collect_hits_with_groups(DEVICE_PATTERNS, content_for_triggers.lower())
        dev_labels, dev_groups = filter_weak_device_hints(dev_labels, dev_groups)
        dev_labels, dev_groups = reconcile_emulator_vs_real(dev_labels, dev_groups)

        trig_labels, trig_groups = collect_hits_with_groups(TRIGGER_PATTERNS_PRIMARY, content_for_triggers.lower())

        if any_match(GHA_GRADLE_INPUTS, content_for_triggers):
            has_gradle_anywhere = bool(
                GRADLE_ANYWHERE_RE.search(content_for_triggers) or
                GRADLE_BUILD_ACTION_RE.search(content_for_triggers)
            )
            tied_to_emulator_action = bool(re.search(
                r'(?mi)^\s*uses\s*:\s*(?:reactivecircus/android-emulator-runner|'
                r'malinskiy/action-android/emulator-run-cmd|'
                r'hannesa2/action-android/emulator-run-cmd)\@',
                content_for_triggers
            ))
            if has_gradle_anywhere or tied_to_emulator_action:
                trig_labels = unique_preserve(trig_labels + ["gha gradle inputs/script"])
                trig_groups = unique_preserve(trig_groups + ["Gradle"])

        if any_match(GHA_GMD_INPUTS, content_for_triggers):
            trig_labels = unique_preserve(trig_labels + ["variant/device AndroidTest"])
            trig_groups = unique_preserve(trig_groups + ["Gradle"])

        flutter_it_hits = []
        for m in FLUTTER_IT_LINE.finditer(content_for_triggers):
            line = m.group(0)
            if FLUTTER_IT_ANDROID_HINT.search(line) or FLUTTER_DEVICE_IS_ANDROID.search(line):
                flutter_it_hits.append(line)

        flutter_it_android_targeted = False
        if flutter_it_hits:
            trig_labels = unique_preserve(trig_labels + ["flutter integration test"])
            trig_groups = unique_preserve(trig_groups + ["Flutter"])
            file_flutter_it_present = True

            for line in flutter_it_hits:
                d = FLUTTER_DEVICE_FLAG_RE.search(line)
                if d:
                    plat = d.group("dev").strip('"\'').lower()
                    if (
                        plat == "android" or
                        plat.startswith("emulator-") or
                        "sdk gphone" in plat or
                        "android sdk built for" in plat or
                        "pixel" in plat
                    ):
                        flutter_it_android_targeted = True
                        if "android" not in file_flutter_devices:
                            file_flutter_devices.append("android")
                    elif plat in {"linux", "macos", "windows"}:
                        if plat not in file_flutter_devices:
                            file_flutter_devices.append(plat)
                    elif plat in {"ios", "iphone", "ipad", "iphone simulator"}:
                        if "ios" not in file_flutter_devices:
                            file_flutter_devices.append("ios")
                    elif plat in {"web", "web-server", "chrome", "edge", "firefox", "safari"}:
                        if "web" not in file_flutter_devices:
                            file_flutter_devices.append("web")

            if (not file_flutter_devices) and LINUX_HEADLESS_HINTS_RE.search(content_for_triggers):
                if "linux" not in file_flutter_devices:
                    file_flutter_devices.append("linux")

        fb_trig_labels, fb_trig_groups = collect_hits_with_groups(TRIGGER_PATTERNS_ANYWHERE, content_for_triggers.lower())
        if fb_trig_labels:
            trig_labels = unique_preserve(trig_labels + fb_trig_labels)
            trig_groups = unique_preserve(trig_groups + fb_trig_groups)

        has_test_trigger = bool(trig_labels)
        android_context = bool(ANDROID_CONTEXT_RE.search(content_for_triggers))
        if ("other gha emulator" in dev_labels) and (not android_context) and (not has_test_trigger):
            dev_labels = [l for l in dev_labels if l != "other gha emulator"]
            if not dev_labels:
                dev_groups = [g for g in dev_groups if g != "Emulator"]

        # STRICT: Android runtime evidence only (no broad ANDROID_CONTEXT_RE)
        has_android_env_strict = has_android_runtime_evidence(dev_labels, dev_groups)
        if flutter_it_hits and (flutter_it_android_targeted or has_android_env_strict):
            file_flutter_it_androidish = True

        file_device_labels  = unique_preserve(file_device_labels  + dev_labels)
        file_device_groups  = unique_preserve(file_device_groups  + dev_groups)
        file_trigger_labels = unique_preserve(file_trigger_labels + trig_labels)
        file_trigger_groups = unique_preserve(file_trigger_groups + trig_groups)

        non_flutter_triggers = [l for l in trig_labels if l.lower() != "flutter integration test"]
        flutter_counts_as_instr = (
            ("flutter integration test" in [l.lower() for l in trig_labels]) and
            (flutter_it_android_targeted or has_android_env_strict)
        )
        has_device_setup = bool(dev_labels)
        instru_t_ci_signal_job = bool(
            non_flutter_triggers or
            flutter_counts_as_instr or
            (has_device_setup and any(g in {"Emulator", "Third_Party_Lab", "Real_Device"} for g in dev_groups))
        )
        file_instru_signal_any = file_instru_signal_any or instru_t_ci_signal_job

    # -------------------------------------------------------------------
    # Follow called files & merge inline + audit metrics
    # -------------------------------------------------------------------
    if FOLLOW_CALLED_FILES and repo_root is not None and Path(repo_root).exists():
        refs_from_yaml = unique_preserve(refs_from_yaml)
        workdirs_from_yaml = unique_preserve(workdirs_from_yaml)

        called_evidence = follow_and_scan_called_files(
            full_name=full_name,
            initial_refs=refs_from_yaml,
            initial_workdirs=workdirs_from_yaml,
            max_depth=MAX_FOLLOW_DEPTH,
        )

        followed_files_count = len(called_evidence.get("scanned_files", []) or [])
        unresolved_dynamic_refs_count = int(called_evidence.get("unresolved_dynamic_refs_count", 0) or 0)
        called_instru_t_ci_signal = called_instru_signal_from_evidence(called_evidence)

        # merge evidence from called files
        file_trigger_labels = unique_preserve(file_trigger_labels + called_evidence.get("trigger_labels", []))
        file_trigger_groups = unique_preserve(file_trigger_groups + called_evidence.get("trigger_groups", []))
        file_device_labels  = unique_preserve(file_device_labels  + called_evidence.get("device_labels", []))
        file_device_groups  = unique_preserve(file_device_groups  + called_evidence.get("device_groups", []))

        if called_evidence.get("flutter_it_present"):
            file_flutter_it_present = True
        if called_evidence.get("flutter_it_androidish"):
            file_flutter_it_androidish = True
        file_flutter_devices = unique_preserve(file_flutter_devices + called_evidence.get("flutter_devices", []))

        if called_evidence.get("env_declared"):
            if (not file_env_declared) or (file_env_location != "inline"):
                file_env_declared = True
                file_env_location = called_evidence.get("env_location", "config") or "config"

        # overall signal: YAML signal OR called-file signal
        file_instru_signal_any = file_instru_signal_any or called_instru_t_ci_signal

    # map invocation & env
    combined_groups = unique_preserve(file_trigger_groups + file_device_groups)
    test_inv_list = map_test_invocations(combined_groups, file_trigger_labels, file_flutter_it_androidish)
    test_inv = ",".join(test_inv_list)

    exec_envs = map_execution_envs(file_device_groups, file_device_labels)
    exec_envs_str = ",".join(exec_envs)

    third_party_label = ""
    if "Third Party" in exec_envs:
        if file_env_declared and file_env_location == "inline":
            third_party_label = "Third-Party Lab — Explicit Inline Env"
        elif file_env_declared and file_env_location == "config":
            third_party_label = "Third-Party Lab — Config-Referenced Env"
        else:
            third_party_label = "Third-Party Lab — Invocation Only"

    exec_env_style = map_exec_env_style(exec_envs)
    test_inv_style = map_test_inv_style(test_inv)

    return {
        "filename": filename,
        "full_name": full_name,
        "ci_platform": ci_platform,

        "instru_t_ci_signal": bool(file_instru_signal_any),
        "called_instru_t_ci_signal": bool(called_instru_t_ci_signal),

        "execution_environment": exec_envs_str,
        "test_invocation": test_inv,

        "flutter_integ_t_signal": bool(file_flutter_it_present),
        "flutter_integ_t_d": ",".join(sorted(set(file_flutter_devices))),

        "third_party_env_label": third_party_label,
        "Exec_Env_Style": exec_env_style,
        "Test_Inv_Style": test_inv_style,

        "followed_files_count": int(followed_files_count),
        "unresolved_dynamic_refs_count": int(unresolved_dynamic_refs_count),
    }

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------

# --- CLI removed: import and call analyze_ci_yaml_file / analyze_repo_workflows instead. ---


def analyze_repo_workflows(repo_root: Path, *, full_name: str) -> List[Dict[str, Any]]:
    """Scan .github/workflows/*.yml(yaml) in a local clone and return per-file records."""
    workflows_dir = repo_root / ".github" / "workflows"
    if not workflows_dir.exists():
        return []
    files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
    out: List[Dict[str, Any]] = []
    for p in files:
        rec = analyze_ci_yaml_file(p, full_name=full_name, repo_root=repo_root, ci_platform="GitHub_Actions")
        rec["workflow_relpath"] = str(p.relative_to(repo_root))
        out.append(rec)
    return out
