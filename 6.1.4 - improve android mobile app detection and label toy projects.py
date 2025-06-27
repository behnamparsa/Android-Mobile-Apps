
import os
import pandas as pd
import requests
from dotenv import load_dotenv
from time import sleep

# === CONFIG ===
INPUT_FILE = r"C:\GitHub\Android-Mobile-Apps\Repo_List_checked_Standard_Android_app.xlsx"
OUTPUT_FILE = r"C:\GitHub\Android-Mobile-Apps\Repo_List_enriched.xlsx"
REQUEST_DELAY = 1  # seconds between GitHub API requests

# === LOAD GITHUB TOKEN ===

load_dotenv("All_Token.env")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    raise ValueError("GITHUB_TOKEN not found in All_Token.env")

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "android-repo-analyzer"
}

# === LOAD DATA ===
df = pd.read_excel(INPUT_FILE)
df = df[(df["has_manifest"] == "yes") & (df["has_activity"] == "yes")].copy()

# === HELPER FUNCTION ===
def fetch_github_metadata(full_name):
    url = f"https://api.github.com/repos/{full_name}"
    try:
        r = requests.get(url, headers=HEADERS)
        if r.status_code != 200:
            return None
        data = r.json()
        readme_url = f"https://api.github.com/repos/{full_name}/readme"
        readme_resp = requests.get(readme_url, headers=HEADERS)
        readme_text = ""
        if readme_resp.status_code == 200:
            readme_text = readme_resp.json().get("content", "")
        return {
            "description": data.get("description", ""),
            "topics": ", ".join(data.get("topics", [])),
            "language": data.get("language", ""),
            "readme": readme_text
        }
    except Exception as e:
        print(f"Error fetching {full_name}: {e}")
        return None

def flag_library_or_demo(name, description, readme):
    keywords = ["library", "demo", "sample", "example", "template", "plugin", "test", "benchmark"]
    text = f"{name} {description} {readme}".lower()
    return any(k in text for k in keywords)

# === ENRICH DATA ===
enriched_data = []
for idx, row in df.iterrows():
    full_name = row["full_name"]
    metadata = fetch_github_metadata(full_name)
    if metadata:
        flagged = flag_library_or_demo(full_name, metadata["description"], metadata["readme"])
        enriched_data.append({
            "full_name": full_name,
            "description": metadata["description"],
            "topics": metadata["topics"],
            "language": metadata["language"],
            "is_library_or_demo": flagged
        })
    else:
        enriched_data.append({
            "full_name": full_name,
            "description": "",
            "topics": "",
            "language": "",
            "is_library_or_demo": False
        })
    print(f"Processed: {full_name}")
    sleep(REQUEST_DELAY)

# === MERGE AND SAVE ===
df_enriched = pd.DataFrame(enriched_data)
df_final = df.merge(df_enriched, on="full_name", how="left")
df_final.to_excel(OUTPUT_FILE, index=False)
print(f"✅ Enriched data saved to: {OUTPUT_FILE}")
