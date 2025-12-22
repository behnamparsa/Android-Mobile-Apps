#!/usr/bin/env python3
"""
pass2_fetch_recent_runs_jobs_steps.py

Option A fix:
- DO NOT call runpy.run_path(...) from inside this file.
- Only run the pass2 logic once via the normal __main__ entrypoint.

Usage:
  python pass2_fetch_recent_runs_jobs_steps.py --tokens-env path/to/tokens.env --dry-run
  python pass2_fetch_recent_runs_jobs_steps.py --tokens-env path/to/tokens.env
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict


# ---------------------------
# Config (adjust if needed)
# ---------------------------

SCRIPT_DIR = Path(__file__).resolve().parent

# Default tokens file: put tokens.env next to this script, OR pass --tokens-env
DEFAULT_TOKENS_ENV_FILE = SCRIPT_DIR / "tokens.env"

# If your pipeline expects specific env keys, list them here (optional).
# Example: ["GITHUB_TOKEN", "CI_TOKEN", "API_KEY"]
REQUIRED_TOKEN_KEYS = []  # <- set this if you want validation

DEFAULT_LOG_DIR = SCRIPT_DIR / "logs"


# ---------------------------
# Helpers
# ---------------------------

def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"pass2_{ts}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.info("Logging to %s", log_path)
    return log_path


def load_env_file(path: Path) -> Dict[str, str]:
    """
    Load a simple .env style file with lines like KEY=VALUE.
    - Ignores blank lines and comments (# ...)
    - Strips surrounding quotes from values ("..." or '...')
    - Sets os.environ[KEY] = VALUE
    Returns dict of loaded key/value pairs.
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(
            f"Tokens env file not found: {path}\n"
            f"Create it or pass --tokens-env to point to the correct file."
        )

    loaded: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()

        # Strip surrounding quotes
        if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
            val = val[1:-1]

        if key:
            os.environ[key] = val
            loaded[key] = val

    return loaded


def load_tokens(tokens_env_file: Path) -> Dict[str, str]:
    """
    Loads tokens from a .env file into os.environ, then returns a dict of tokens.
    If REQUIRED_TOKEN_KEYS is empty, returns all keys loaded from the file.
    """
    loaded = load_env_file(tokens_env_file)

    if REQUIRED_TOKEN_KEYS:
        missing = [k for k in REQUIRED_TOKEN_KEYS if not os.environ.get(k)]
        if missing:
            raise RuntimeError(
                "Missing required token keys in environment: "
                + ", ".join(missing)
                + f"\nChecked file: {tokens_env_file}"
            )
        return {k: os.environ[k] for k in REQUIRED_TOKEN_KEYS}

    # If you didn't specify REQUIRED_TOKEN_KEYS, just return whatever was in the env file.
    return dict(loaded)


# ---------------------------
# Main pass2 runner (NO runpy recursion!)
# ---------------------------

def run_pass2(tokens_env_file: Path, dry_run: bool = False) -> int:
    """
    Runs pass2 once. No runpy.run_path() here (Option A).
    """
    logging.info("Starting pass2")
    logging.info("Tokens env file: %s", tokens_env_file)

    tokens = load_tokens(tokens_env_file)

    if dry_run:
        logging.info("Dry-run enabled. Loaded %d keys from tokens file.", len(tokens))
        if tokens:
            logging.info("Loaded keys: %s", ", ".join(sorted(tokens.keys())))
        else:
            logging.info("No keys loaded (file may be empty or only comments).")
        logging.info("Exiting dry-run successfully.")
        return 0

    # ------------------------------------------------------------
    # DO YOUR PASS2 WORK HERE
    #
    # Replace this section with your actual logic that fetches
    # recent runs / jobs / steps, etc.
    # ------------------------------------------------------------
    logging.info("Running pass2 work...")

    # Example placeholder (safe, no recursion):
    # fetch_recent_runs_jobs_steps(tokens)
    #
    # For now, we just confirm we got tokens loaded and exit.
    logging.info("Pass2 completed (placeholder). Implement your pass2 logic here.")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--tokens-env",
        type=Path,
        default=DEFAULT_TOKENS_ENV_FILE,
        help="Path to tokens.env (default: tokens.env next to this script)",
    )
    p.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Directory to write logs (default: ./logs next to this script)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Only load/validate tokens and exit (no pipeline work)",
    )
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    setup_logging(args.log_dir)

    try:
        return run_pass2(tokens_env_file=args.tokens_env, dry_run=args.dry_run)
    except Exception as e:
        logging.exception("Pass2 failed: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
