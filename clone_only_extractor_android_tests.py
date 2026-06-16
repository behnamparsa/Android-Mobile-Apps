# -*- coding: utf-8 -*-
"""
Clone-only extractor for Android instrumentation-testing related files.

✅ Guaranteed unchanged: YAML detection & download (same ci_patterns and matching rule as your original).

Aligned behavior (everywhere else):
- Detect default branch via `git ls-remote --symref <repo> HEAD` (fallback to main/master)
- Shallow clone that branch (--depth 1)
- Extract into TWO buckets only:

  All_Config_Files:
    * CI YAML (ROOT/non-ROOT EXACTLY per your original ci_patterns + matching rule)
    * Shell/runner scripts: .sh .bash .zsh .ksh .bat .cmd .ps1 .psm1 .psd1 + Makefile/makefile/GNUmakefile
    * Gradle & settings: *.gradle *.gradle.kts gradle.properties settings.gradle(.kts)
    * AndroidManifest.xml (ANY path) **only if** it contains at least one <activity
    * Likely CI JSON (allowlist): android-studio-loading.json, saucectl.config.json, firebase.json, test-lab.json
    * Flutter config: pubspec.yaml

  All_Test_Files:
    * Native Android instrumentation sources: any src/**AndroidTest**/*.kt|*.java (case-insensitive; supports flavors)
    * Flutter integration tests: integration_test/**/*.dart, test_driver/**/*.dart

- Flat filenames (collision-safe) using LOWERCASE **filename only**:
  {owner}.{project}__{ci_platform}++{file_lower}
  If a collision would occur in the bucket, append a counter BEFORE the extension:
  {name}.ext, {name}__2.ext, {name}__3.ext, ...

- No H/R/I labels.
- CSV index columns:
  owner, repo, repo_url, default_branch, commit_sha, relative_path, filename,
  flat_filename (actual saved name), ci_platform, html_url, saved_to, bucket, components, ci_root
  (For YAMLs we set ci_root="yes", others blank)

- Project metadata via GitHub API + paginated counts (kept)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import random
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Optional, Set, Tuple
from urllib.parse import urlparse

import pandas as pd
import requests
from dotenv import load_dotenv, set_key

# Ensure robust UTF-8 behavior across environments
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"


# ========= CLI =========

def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Clone-only extractor for Android instrumentation-testing related files",
		formatter_class=argparse.ArgumentDefaultsHelpFormatter,
	)
	parser.add_argument(
		"--csv",
		dest="csv_path",
		type=str,
		default="/workspace/URL_List_Rand.csv",
		help="Path to a CSV containing a 'github_url' column",
	)
	parser.add_argument(
		"--base-dir",
		dest="base_dir",
		type=str,
		default="/workspace/Android_Clone_Extractor",
		help="Base output directory for clones, buckets and logs",
	)
	parser.add_argument(
		"--env-file",
		dest="env_file",
		type=str,
		default="/workspace/All_tokens.env",
		help=".env file containing GitHub tokens as GITHUB_TOKEN_1..GITHUB_TOKEN_6",
	)
	parser.add_argument(
		"--max-projects",
		dest="max_projects",
		type=int,
		default=4697,
		help="Upper bound on projects to process",
	)
	parser.add_argument(
		"--start-number",
		dest="start_number",
		type=int,
		default=1,
		help="1-based starting index in the CSV",
	)
	parser.add_argument(
		"--random-seed",
		dest="random_seed",
		type=int,
		default=42,
		help="Random seed for sampling",
	)
	parser.add_argument(
		"--num-samples",
		dest="num_samples",
		type=int,
		default=150,
		help="Number of sample repos to keep if SAMPLE_LIST is not set in env",
	)
	return parser.parse_args()


# ========= CONFIG =========

def run(cmd, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess[str]:
	"""Run a subprocess command with UTF-8 text output and merged stderr/stdout."""
	return subprocess.run(
		cmd,
		cwd=str(cwd) if cwd else None,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		text=True,
		encoding="utf-8",
		errors="replace",
		check=check,
	)


def split_stem_and_suffixes(name: str) -> tuple[str, str]:
	p = Path(name)
	suffixes = ''.join(p.suffixes)
	if suffixes:
		return name[:-len(suffixes)], suffixes
	return name, ""


def _counter_candidate(base_name: str, n: int) -> str:
	if n == 1:
		return base_name
	if "++" in base_name:
		head, tail = base_name.split("++", 1)
		tail_stem, tail_suf = split_stem_and_suffixes(tail)
		return f"{head}++{tail_stem}__{n}{tail_suf}"
	stem, suf = split_stem_and_suffixes(base_name)
	return f"{stem}__{n}{suf}"


def resolve_counter_name(bucket_dir: Path, base_name: str, in_memory_taken: Set[str]) -> str:
	n = 1
	while True:
		candidate = _counter_candidate(base_name, n)
		if candidate not in in_memory_taken and not (bucket_dir / candidate).exists():
			in_memory_taken.add(candidate)
			return candidate
		n += 1


def sanitize_token(s: str) -> str:
	return re.sub(r"[^a-z0-9._+-]", "_", s.lower())


def file_sha1_hex(path: Path, chunk_size: int = 1 << 20) -> str:
	h = hashlib.sha1()
	with open(path, "rb") as f:
		while True:
			b = f.read(chunk_size)
			if not b:
				break
			h.update(b)
	return h.hexdigest()


def make_flat_filename(owner: str, project: str, ci_platform: str, file_name: str, used_names_set: Set[str]) -> str:
	owner_tok = sanitize_token(owner)
	project_tok = sanitize_token(project)
	ci_tok = sanitize_token(ci_platform or "other")
	file_lower = file_name.lower()
	return f"{owner_tok}.{project_tok}__{ci_tok}++{file_lower}"


def save_file(bucket_dir: Path, flat_filename: str, src: Path, in_memory_taken: Set[str]) -> tuple[Path, str]:
	bucket_dir.mkdir(parents=True, exist_ok=True)
	final_name = resolve_counter_name(bucket_dir, flat_filename, in_memory_taken)
	dest = bucket_dir / final_name
	content_sha1 = file_sha1_hex(src)
	shutil.copy2(src, dest)
	return dest, content_sha1


# ---- Classification helpers (PATH/NAME ONLY, no content except Manifest check) ----
SHELL_EXTS: set[str] = {".sh", ".bash", ".zsh", ".ksh", ".bat", ".cmd", ".ps1", ".psm1", ".psd1"}
MAKEFILES: set[str] = {"makefile", "gnumakefile", "makefile.win", "makefile.mak"}


def is_shell_or_make(file_path: Path) -> tuple[bool, str]:
	name = file_path.name.lower()
	ext = file_path.suffix.lower()
	if name in MAKEFILES:
		return True, "shell"
	if ext in SHELL_EXTS:
		if ext in {".ps1", ".psm1", ".psd1"}:
			return True, "shell_ps"
		if ext in {".bat", ".cmd"}:
			return True, "shell_win"
		return True, "shell"
	return False, ""


def is_gradle_or_settings(file_path: Path) -> tuple[bool, str]:
	n = file_path.name.lower()
	if n.endswith(".gradle") or n.endswith(".gradle.kts"):
		return True, "gradle"
	if n in {"gradle.properties", "settings.gradle", "settings.gradle.kts"}:
		return True, "gradle"
	return False, ""


def is_manifest(file_path: Path) -> bool:
	return file_path.name.lower() == "androidmanifest.xml"


def manifest_has_activity(file_path: Path) -> bool:
	try:
		txt = file_path.read_text(encoding="utf-8", errors="ignore")
		return re.search(r"<\s*activity\b", txt, re.IGNORECASE) is not None
	except Exception:
		return False


# ========= Allowlisted CI JSON filenames =========
LIKELY_CI_JSON: set[str] = {
	"android-studio-loading.json",
	"saucectl.config.json",
	"firebase.json",
	"test-lab.json",
}


def is_allowlisted_ci_json(file_path: Path) -> tuple[bool, str]:
	n = file_path.name.lower()
	if n in LIKELY_CI_JSON:
		if n.startswith("saucectl"):
			return True, "sauce_labs"
		if n.startswith("android-studio"):
			return True, "android_studio"
		if n in {"firebase.json", "test-lab.json"}:
			return True, "firebase_test_lab"
		return True, "ci_json"
	return False, ""


def is_flutter_pubspec(file_path: Path) -> bool:
	return file_path.name.lower() == "pubspec.yaml"


def is_flutter_test(file_path: Path) -> tuple[bool, str]:
	rel = file_path.as_posix().lower()
	if rel.startswith("integration_test/") or "/integration_test/" in rel:
		return True, "androidtest_dart"
	if rel.startswith("test_driver/") or "/test_driver/" in rel:
		return True, "androidtest_dart"
	return False, ""


def is_androidtest_code(file_path: Path) -> tuple[bool, str]:
	rel = file_path.as_posix().lower()
	if "/src/" in rel and "androidtest" in rel:
		if file_path.suffix.lower() == ".kt":
			return True, "androidtest_kotlin"
		if file_path.suffix.lower() == ".java":
			return True, "androidtest_java"
	return False, ""


# === CI patterns (UNCHANGED from your original logic) ===
ci_patterns = {
	r"\\.travis\\.yml$": "Travis_CI",
	r"\\.appveyor\\.yml$": "AppVeyor",
	r"appveyor\\.yml$": "AppVeyor",
	r"circle\\.yml$": "Circle_CI",
	r"\\.circleci/config\\.yml$": "Circle_CI",
	r"azure-pipelines\\.yml$": "Azure_Pipelines",
	r"\\.github/workflows/.*\\.(yml|yaml)$": "GitHub_Actions",
	r"bitbucket-pipelines\\.yml$": "Bitbucket",
	r"\\.gitlab-ci\\.yml$": "GitLab",
	r"Jenkinsfile\\.yml$": "Jenkins",
	r"bitrise\\.yml$": "Bitrise",
	r"bamboo\\.yml$": "Bamboo",
	r"codeship-services\\.yml$": "Codeship",
	r"\\.gocd\\.yaml$": "GoCD",
	r"\\.cirrus\\.yml$": "Cirrus",
	r"wercker\\.yaml$": "Wercker",
	r"semaphore\\.yml$": "Semaphore",
	r"codemagic\\.yaml$": "Nevercode",
}

# Normalize provider token in flat filename
ci_provider_token = {
	"GitHub_Actions": "github_actions",
	"GitLab": "gitlab",
	"Circle_CI": "circle_ci",
	"Azure_Pipelines": "azure_pipelines",
	"Travis_CI": "travis_ci",
	"Bitrise": "bitrise",
	"Bitbucket": "bitbucket",
	"Jenkins": "jenkins",
	"Bamboo": "bamboo",
	"Codeship": "codeship",
	"GoCD": "gocd",
	"Cirrus": "cirrus",
	"Wercker": "wercker",
	"Semaphore": "semaphore",
	"Nevercode": "codemagic",
	"AppVeyor": "appveyor",
	"Other": "other",
}

# ========= CSV index fields =========
INDEX_FIELDS = [
	"owner", "repo", "repo_url", "default_branch", "commit_sha",
	"relative_path", "filename", "flat_filename", "ci_platform",
	"html_url", "saved_to", "bucket", "components", "ci_root"
]


# ========= Main =========

def main() -> None:
	args = parse_args()

	# === LOAD .env ===
	ENV_FILE = args.env_file
	load_dotenv(ENV_FILE)

	# Load GitHub tokens
	TOKENS = [os.getenv(f"GITHUB_TOKEN_{i}") for i in range(1, 7)]
	TOKENS = [t for t in TOKENS if t]
	if not TOKENS:
		print("⚠️ Warning: No GitHub tokens found in All_tokens.env (API metadata might be rate limited).")
	token_index = 0  # For rotation

	START_NUMBER = args.start_number
	SAMPLE_LIST_RAW = os.getenv("SAMPLE_LIST", "").strip()

	# === PATHS ===
	csv_path = Path(args.csv_path).resolve()
	base_dir = Path(args.base_dir).resolve()

	clone_dir = base_dir / "Cloned repos"
	cloned_sample_dir = base_dir / "Cloned_Sample"

	# NEW buckets
	config_bucket = base_dir / "All_Config_Files"
	tests_bucket = base_dir / "All_Test_Files"

	# Metadata and logs
	commits_dir = base_dir / "Commits"
	git_metadata_dir = base_dir / "Git_Metadata"
	metadata_path = base_dir / "Project_Metadata.csv"

	# Backward-compat list (we’ll still fill it with richer fields)
	list_of_config_path = base_dir / "List_of_Config.csv"

	# Unified flat index for saved files
	flat_index_csv = base_dir / "All_Config_Index.csv"

	# === ENSURE ALL FOLDERS EXIST ===
	for path in [clone_dir, commits_dir, git_metadata_dir, cloned_sample_dir, config_bucket, tests_bucket]:
		path.mkdir(parents=True, exist_ok=True)

	# ========= LOAD AND CLEAN CSV =========
	if not csv_path.exists():
		raise FileNotFoundError(f"Input CSV not found: {csv_path}")

	df = pd.read_csv(csv_path)
	df.columns = df.columns.str.strip().str.lower()
	candidate_cols = [c for c in df.columns if c == "github_url"] or [
		c for c in df.columns if ("github" in c and "url" in c)
	]
	if not candidate_cols:
		raise KeyError("CSV must contain a 'github_url' column (case-insensitive)")
	url_col = candidate_cols[0]
	df = df[df[url_col].notna()]
	df[url_col] = df[url_col].astype(str).str.strip()
	df = df[df[url_col].str.startswith("https://")]
	(df[[url_col]].rename(columns={url_col: "github_url"})).to_csv(base_dir / 'Sorted_URL_List.csv', index_label='Index')

	# ========= Sampling =========
	if SAMPLE_LIST_RAW:
		sample_indices_to_keep: set[int] = set(map(int, SAMPLE_LIST_RAW.split(',')))
		print(f"🔁 Loaded SAMPLE_LIST from .env with {len(sample_indices_to_keep)} indices.")
	else:
		random.seed(args.random_seed)
		sample_indices_to_keep = set(random.sample(range(len(df)), min(args.num_samples, len(df))))
		sample_string = ",".join(map(str, sorted(sample_indices_to_keep)))
		set_key(ENV_FILE, 'SAMPLE_LIST', sample_string)
		print(f"🎲 Generated and saved new SAMPLE_LIST with {len(sample_indices_to_keep)} indices.")

	# ========= CSV: create flat index header if missing =========
	if not flat_index_csv.exists():
		with open(flat_index_csv, "w", newline="", encoding="utf-8") as f:
			csv.DictWriter(f, fieldnames=INDEX_FIELDS).writeheader()

	# ========= PROCESS EACH REPO =========
	review_status_rows: list[dict] = []
	CLONE_FAILURE_COLUMNS = ["repo_index", "repo_name", "github_url", "error_message"]

	for i in range(START_NUMBER - 1, min(len(df), args.max_projects)):
		url = df.iloc[i][url_col]
		owner_repo = urlparse(url).path.strip("/").split("/")
		if len(owner_repo) < 2:
			continue
		owner, project = owner_repo[0], owner_repo[1].replace(".git", "")
		repo_index = str(i).zfill(4)
		repo_name = f"{repo_index}.{owner}.{project}"
		repo_path = clone_dir / repo_name

		print(f"\n🔍 [{i+1}/{len(df)}] Processing {repo_name}...")

		# --- Detect default branch via ls-remote, then shallow clone that branch ---
		try:
			default_branch: Optional[str] = None
			try:
				out = run(["git", "ls-remote", "--symref", url, "HEAD"]).stdout
				for line in out.splitlines():
					s = line.strip()
					if s.startswith("ref: ") and s.endswith("HEAD"):
						ref = s.split()[1]
						if ref.startswith("refs/heads/"):
							default_branch = ref.split("/", 2)[2]
							break
			except Exception:
				default_branch = None

			if default_branch is None:
				for guess in ("main", "master"):
					try:
						run(["git", "ls-remote", url, f"refs/heads/{guess}"])
						default_branch = guess
						break
					except Exception:
						pass
			if default_branch is None:
				raise RuntimeError("Could not determine default branch (ls-remote)")

			print(f"📌 Default branch: {default_branch}")
			run(["git", "clone", "--depth", "1", "--single-branch", "--branch", default_branch, url, str(repo_path)])
			print("✅ Clone complete")
		except Exception as e:
			error_message = (str(e) or "Unknown error").strip()
			print(f"❌ Clone failed for {repo_name}\n{error_message}")
			review_status_rows.append({"html_url": url.strip(), "clone_status": "no", "yml_detected": "no"})
			pd.DataFrame([review_status_rows[-1]]).to_csv(
				base_dir / "Clone_Status.csv",
				mode='a',
				header=not (base_dir / "Clone_Status.csv").exists(),
				index=False
			)
			fail_row = {"repo_index": repo_index, "repo_name": repo_name, "github_url": url.strip(), "error_message": error_message}
			fail_path = base_dir / "Clone_Failures.csv"
			pd.DataFrame([fail_row])[CLONE_FAILURE_COLUMNS].to_csv(
				fail_path, mode='a', header=not fail_path.exists(), index=False
			)
			continue

		# --- Commit count + HEAD SHA ---
		try:
			local_commit_count = int(run(['git', '-C', str(repo_path), 'rev-list', '--count', 'HEAD']).stdout.strip() or "0")
		except subprocess.CalledProcessError:
			local_commit_count = 0
			print(f"⚠️ Could not get commit count for {repo_name}")

		try:
			head_sha = run(['git', '-C', str(repo_path), 'rev-parse', '--verify', 'HEAD']).stdout.strip()
		except subprocess.CalledProcessError:
			head_sha = ""

		# --- Optional: extract commit metadata (kept, fixed) ---
		if local_commit_count > 0:
			try:
				commit_hashes = run(["git", "-C", str(repo_path), "log", "--pretty=format:%H"]).stdout.strip().splitlines()
				rows: list[dict[str, str]] = []
				for commit in commit_hashes:
					cp = run([
						"git", "-C", str(repo_path), "show", "--quiet",
						"--pretty=format:%H|%an|%ae|%ad|%s", "--date=iso", commit
					], check=False)
					line = (cp.stdout or "").strip()
					if not line:
						continue
					parts = line.split("|", 4)
					if len(parts) < 5:
						continue
					commit_hash, author_name, author_email, commit_date, commit_message = parts
					rows.append({
						"commit_hash": commit_hash,
						"author_name": author_name,
						"author_email": author_email,
						"commit_date": commit_date,
						"commit_message": commit_message,
					})
				if rows:
					dfc = pd.DataFrame(rows)
					git_metadata_dir.mkdir(parents=True, exist_ok=True)
					flat_filename_meta = f"{project}__GitMetadata++contributors_commits.csv"
					dfc.to_csv(git_metadata_dir / flat_filename_meta, index=False)
			except subprocess.CalledProcessError as e:
				print(f"❌ Failed to extract commit data for {repo_path.name}: {e}")

		# --- Walk tree & save files into two buckets ---
		any_yml = False
		legacy_rows: list[dict] = []
		used_names_config: Set[str] = set()
		used_names_tests: Set[str] = set()
		seen_realpaths: Set[Path] = set()

		for root, _, files in os.walk(repo_path):
			for file in files:
				file_path = Path(root) / file
				try:
					realp = file_path.resolve()
				except Exception:
					realp = file_path
				if realp in seen_realpaths:
					continue
				seen_realpaths.add(realp)

				rel_path = str(file_path.relative_to(repo_path)).replace("\\", "/")
				filename_lower = file.lower()
				dest_path = None
				saved_name = None
				ci_platform = "Other"
				bucket = ""
				ci_root_value = ""  # set "yes" only for YAMLs we save

				try:
					# --- 1) YAML (UNCHANGED LOGIC) ---
					if filename_lower.endswith(('.yml', '.yaml')):
						matched_ci_type = None
						for pattern, platform in ci_patterns.items():
							if re.search(pattern, rel_path, re.IGNORECASE):
								matched_ci_type = platform
								break
						if matched_ci_type:
							ci_platform = ci_provider_token.get(matched_ci_type, "other")
							flat_filename = make_flat_filename(owner, project, ci_platform, filename_lower, used_names_config)
							dest_path, content_sha1 = save_file(config_bucket, flat_filename, file_path, used_names_config)
							saved_name = dest_path.name
							bucket = "All_Config_Files"
							any_yml = True
							ci_root_value = "yes"  # we only log this for YAML
						else:
							# skip unmatched YAMLs exactly like your current behavior
							continue

					# --- 2) Shell & Makefiles (CONFIG) ---
					elif is_shell_or_make(file_path)[0]:
						ci_platform = is_shell_or_make(file_path)[1]
						flat_filename = make_flat_filename(owner, project, ci_platform, filename_lower, used_names_config)
						dest_path, content_sha1 = save_file(config_bucket, flat_filename, file_path, used_names_config)
						saved_name = dest_path.name
						bucket = "All_Config_Files"

					# --- 3) Gradle & settings (CONFIG) ---
					elif is_gradle_or_settings(file_path)[0]:
						ci_platform = "gradle"
						flat_filename = make_flat_filename(owner, project, ci_platform, filename_lower, used_names_config)
						dest_path, content_sha1 = save_file(config_bucket, flat_filename, file_path, used_names_config)
						saved_name = dest_path.name
						bucket = "All_Config_Files"

					# --- 4) AndroidManifest with <activity> (CONFIG) ---
					elif is_manifest(file_path) and manifest_has_activity(file_path):
						ci_platform = "manifest_activity"
						flat_filename = make_flat_filename(owner, project, ci_platform, filename_lower, used_names_config)
						dest_path, content_sha1 = save_file(config_bucket, flat_filename, file_path, used_names_config)
						saved_name = dest_path.name
						bucket = "All_Config_Files"

					# --- 5) Allowlisted CI JSON (CONFIG) ---
					elif is_allowlisted_ci_json(file_path)[0]:
						ci_platform = is_allowlisted_ci_json(file_path)[1]
						flat_filename = make_flat_filename(owner, project, ci_platform, filename_lower, used_names_config)
						dest_path, content_sha1 = save_file(config_bucket, flat_filename, file_path, used_names_config)
						saved_name = dest_path.name
						bucket = "All_Config_Files"

					# --- 6) Flutter pubspec.yaml (CONFIG) ---
					elif is_flutter_pubspec(file_path):
						ci_platform = "flutter"
						flat_filename = make_flat_filename(owner, project, ci_platform, filename_lower, used_names_config)
						dest_path, content_sha1 = save_file(config_bucket, flat_filename, file_path, used_names_config)
						saved_name = dest_path.name
						bucket = "All_Config_Files"

					# --- 7) Flutter integration tests (TESTS) ---
					elif is_flutter_test(file_path)[0]:
						ci_platform = "androidtest_dart"
						flat_filename = make_flat_filename(owner, project, ci_platform, filename_lower, used_names_tests)
						dest_path, content_sha1 = save_file(tests_bucket, flat_filename, file_path, used_names_tests)
						saved_name = dest_path.name
						bucket = "All_Test_Files"

					# --- 8) AndroidTest code .kt/.java (TESTS) ---
					elif is_androidtest_code(file_path)[0]:
						ci_platform = is_androidtest_code(file_path)[1]
						flat_filename = make_flat_filename(owner, project, ci_platform, filename_lower, used_names_tests)
						dest_path, content_sha1 = save_file(tests_bucket, flat_filename, file_path, used_names_tests)
						saved_name = dest_path.name
						bucket = "All_Test_Files"

					else:
						# all other files ignored
						continue

					# === Write unified CSV index (actual saved filename) ===
					with open(flat_index_csv, "a", newline="", encoding="utf-8") as f:
						writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
						writer.writerow({
							"owner": owner,
							"repo": project,
							"repo_url": url.strip(),
							"default_branch": default_branch,
							"commit_sha": head_sha,
							"relative_path": rel_path,
							"filename": file,
							"flat_filename": saved_name,
							"ci_platform": ci_platform,
							"html_url": f"https://github.com/{owner}/{project}/blob/{default_branch}/{rel_path}",
							"saved_to": str(dest_path),
							"bucket": bucket,
							"components": "",
							"ci_root": ci_root_value,
						})

					# === Maintain legacy List_of_Config.csv with richer columns + content hash ===
					legacy_rows.append({
						"html_url": url.strip().rstrip('/'),
						"repo_name": repo_name,
						"owner": owner,
						"repo": project,
						"default_branch": default_branch,
						"commit_sha": head_sha,
						"config_file_path": saved_name,        # actual saved name
						"original_rel_path": rel_path,         # repo-relative path
						"original_file_name": file,            # explicit file name
						"file_name": file,                     # backward compat
						"file_type": filename_lower.split(".")[-1] if "." in filename_lower else filename_lower,
						"bucket": bucket,
						"ci_platform": ci_platform,
						"content_sha1": content_sha1,          # 40-hex SHA-1 of file bytes
						"ci_root": ci_root_value,
					})

				except Exception as e:
					# non-fatal: keep going
					print(f"⚠️ Could not process or copy {rel_path} in {repo_name}: {e}")

		# If no config YAML files found after scanning repo
		review_status_row = {
			"html_url": url.strip(),
			"clone_status": "yes",
			"yml_detected": "yes" if any_yml else "no"
		}
		pd.DataFrame([review_status_row]).to_csv(
			base_dir / "Clone_Status.csv", mode='a', header=not (base_dir / "Clone_Status.csv").exists(), index=False
		)

		# Persist legacy List_of_Config.csv
		if legacy_rows:
			ldf = pd.DataFrame(legacy_rows)
			if list_of_config_path.exists():
				ldf.to_csv(list_of_config_path, mode='a', header=False, index=False)
			else:
				ldf.to_csv(list_of_config_path, mode='w', header=True, index=False)

		# === Fetch and save metadata + paginated counts (kept) ===
		try:
			def get_count(api_url: str, headers: dict[str, str]) -> int:
				per_page = 100
				page = 1
				total_items = 0
				try:
					while True:
						response = requests.get(api_url, headers=headers, params={"per_page": per_page, "page": page}, timeout=30)
						if response.status_code != 200:
							break
						items = response.json()
						if not isinstance(items, list):
							break
						total_items += len(items)
						if len(items) < per_page:
							break
						page += 1
				except Exception:
					pass
				return total_items

			headers: dict[str, str] = {}
			if TOKENS:
				headers = {'Authorization': f'token {TOKENS[token_index % len(TOKENS)]}'}
				token_index += 1
			base_api = f"https://api.github.com/repos/{owner}/{project}"
			r = requests.get(base_api, headers=headers, timeout=30)
			data = r.json() if r.status_code == 200 else {}

			if TOKENS:
				headers = {'Authorization': f'token {TOKENS[token_index % len(TOKENS)]}'}
				token_index += 1
			contributors_count = get_count(f"{base_api}/contributors", headers)

			if TOKENS:
				headers = {'Authorization': f'token {TOKENS[token_index % len(TOKENS)]}'}
				token_index += 1
			pulls_count = get_count(f"{base_api}/pulls?state=all", headers)

			if TOKENS:
				headers = {'Authorization': f'token {TOKENS[token_index % len(TOKENS)]}'}
				token_index += 1
			commits_count = get_count(f"{base_api}/commits", headers)

			metadata_row = {
				"html_url": url,
				"repo_index": repo_index,
				"repo_name": repo_name,
				"id": data.get("id"),
				"name": data.get("name"),
				"full_name": data.get("full_name"),
				"owner": data.get("owner", {}).get("login") if data.get("owner") else None,
				"private": data.get("private"),
				"fork": data.get("fork"),
				"created_at": data.get("created_at"),
				"updated_at": data.get("updated_at"),
				"pushed_at": data.get("pushed_at"),
				"homepage": data.get("homepage"),
				"size": data.get("size"),
				"stargazers_count": data.get("stargazers_count"),
				"language": data.get("language"),
				"forks_count": data.get("forks_count"),
				"open_issues_count": data.get("open_issues_count"),
				"license": data.get("license", {}).get("name") if data.get("license") else None,
				"topics": ", ".join(data.get("topics", [])) if data.get("topics") else None,
				"visibility": data.get("visibility"),
				"default_branch": data.get("default_branch"),
				"has_issues": data.get("has_issues"),
				"has_projects": data.get("has_projects"),
				"has_downloads": data.get("has_downloads"),
				"has_wiki": data.get("has_wiki"),
				"has_pages": data.get("has_pages"),
				"archived": data.get("archived"),
				"disabled": data.get("disabled"),
				"allow_forking": data.get("allow_forking"),
				"is_template": data.get("is_template"),
				"web_commit_signoff_required": data.get("web_commit_signoff_required"),
				"contributors": contributors_count,
				"pull_requests": pulls_count,
				"commits_GitAPI": commits_count,
				"local_commit_count": local_commit_count
			}

			metadata_df = pd.DataFrame([metadata_row])
			if metadata_path.exists():
				metadata_df.to_csv(metadata_path, mode='a', header=False, index=False)
			else:
				metadata_df.to_csv(metadata_path, mode='w', header=True, index=False)

			# Save contributor names
			if TOKENS:
				headers = {'Authorization': f'token {TOKENS[token_index % len(TOKENS)]}'}
				token_index += 1
			contrib_url = f"{base_api}/contributors"
			r_contrib = requests.get(contrib_url, headers=headers, timeout=30)
			if r_contrib.status_code == 200:
				contributor_logins = [c['login'] for c in r_contrib.json()]
				contributors_text = "\n".join(contributor_logins)
				contributors_filename = f"{owner}.{project}__Contributors++list.txt"
				contributors_path = commits_dir / contributors_filename
				with open(contributors_path, "w", encoding="utf-8") as f:
					f.write(contributors_text)
		except Exception as e:
			print(f"⚠️ Metadata or contributors error for {repo_name}: {e}")

		# === Move to Cloned_Sample or delete ===
		# try:
		#     if i in sample_indices_to_keep:
		#         dest_path = cloned_sample_dir / repo_path.name
		#         if dest_path.exists():
		#             shutil.rmtree(dest_path, ignore_errors=True)
		#         shutil.move(str(repo_path), str(dest_path))
		#         print(f"📆 Sample repo moved to: {dest_path}")
		#     else:
		#         def force_remove_readonly(func, path, _):
		#             os.chmod(path, stat.S_IWRITE)
		#             func(path)
		#         shutil.rmtree(repo_path, onerror=force_remove_readonly)
		#         print(f"🕵️ Deleted cloned repo: {repo_name}")
		# except Exception as e:
		#     print(f"❌ Error handling repo folder for {repo_name}: {e}")

		# set_key(ENV_FILE, 'START_NUMBER', str(i + 2))

	# === FINAL DEDUPLICATION OF LOG FILES ===
	for p in [
		base_dir / "List_of_Config.csv",
		base_dir / "Clone_Failures.csv",
		base_dir / "Project_Metadata.csv",
		base_dir / "Clone_Status.csv",
		flat_index_csv,
	]:
		if p.exists():
			try:
				dfp = pd.read_csv(p)
				dfp.drop_duplicates().to_csv(p, index=False)
			except Exception:
				pass

	print("\n✅ Process complete. Two-bucket save aligned, YAML logic unchanged.")


if __name__ == "__main__":
	main()