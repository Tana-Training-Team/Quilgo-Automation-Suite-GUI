"""
Manatal API diagnostic script.
Saves raw API responses to Manatal_test_api/ so you can inspect what the API
actually returns — especially stage IDs, job details, and candidate matches.

Run from the project root:
    python testmanatalapi.py
"""

import configparser
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT_ROOT / 'gui_config.ini'
OUTPUT_DIR = PROJECT_ROOT / 'Manatal_test_api'
BASE_URL = "https://api.manatal.com/open/v3"
JOB_ID = 2619874          # same as manatal_fetcher.py
MAX_MATCH_PAGES = 5        # cap to avoid hammering the API during a test run


def load_api_key() -> str:
    if not CONFIG_FILE.exists():
        sys.exit(f"❌ Config file not found: {CONFIG_FILE}")
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    key = cfg.get('credentials', 'manatal_api_key', fallback='').strip()
    if not key:
        sys.exit("❌ 'manatal_api_key' not found in gui_config.ini [credentials].")
    return key


def save(data, filename: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = OUTPUT_DIR / filename
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  💾 Saved → {filepath.relative_to(PROJECT_ROOT)}")
    return filepath


def get(url: str, headers: dict) -> dict | list | None:
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        print(f"  ⚠️  Request failed for {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    print("=" * 60)
    print("  Manatal API Diagnostic Tool")
    print(f"  Output folder : Manatal_test_api/")
    print(f"  Timestamp     : {timestamp}")
    print("=" * 60)

    api_key = load_api_key()
    headers = {"accept": "application/json", "Authorization": f"Token {api_key}"}
    print(f"\n✔ API key loaded from gui_config.ini (last 6 chars: ...{api_key[-6:]})")

    # ------------------------------------------------------------------
    # 1. Job details
    # ------------------------------------------------------------------
    print(f"\n[1/4] Fetching job details for job ID {JOB_ID}...")
    job_data = get(f"{BASE_URL}/jobs/{JOB_ID}/", headers)
    if job_data:
        save(job_data, f"{timestamp}_job_details.json")
        print(f"      Job title : {job_data.get('name', 'N/A')}")
        print(f"      Status    : {job_data.get('status', 'N/A')}")

    # ------------------------------------------------------------------
    # 2. Pipeline stages
    # ------------------------------------------------------------------
    print(f"\n[2/4] Fetching pipeline stages for job {JOB_ID}...")
    stages_data = get(f"{BASE_URL}/jobs/{JOB_ID}/stages/", headers)
    if stages_data:
        save(stages_data, f"{timestamp}_pipeline_stages.json")
        stages_list = stages_data if isinstance(stages_data, list) else stages_data.get('results', [])
        if stages_list:
            print(f"      Found {len(stages_list)} stage(s):")
            for s in stages_list:
                print(f"        ID {s.get('id'):>10}  →  {s.get('name', 'N/A')}")
        else:
            print("      No stages returned (endpoint may differ — check the raw file).")

    # ------------------------------------------------------------------
    # 3. First N pages of matches — capture stage distribution
    # ------------------------------------------------------------------
    print(f"\n[3/4] Fetching up to {MAX_MATCH_PAGES} pages of matches...")
    all_matches = []
    stage_counts: dict[str, int] = {}   # stage_name → count
    stage_ids: dict[int, str] = {}       # stage_id   → stage_name
    next_url = f"{BASE_URL}/jobs/{JOB_ID}/matches/?page_size=100"
    page = 0

    while next_url and page < MAX_MATCH_PAGES:
        page += 1
        print(f"      Page {page}…", end=" ", flush=True)
        data = get(next_url, headers)
        if data is None:
            print("failed.")
            break
        results = data.get('results', [])
        print(f"{len(results)} matches.")
        all_matches.extend(results)

        for m in results:
            stage = m.get('stage') or {}
            sid   = stage.get('id', 'unknown')
            sname = stage.get('name', 'unknown')
            stage_ids[sid] = sname
            key = f"[{sid}] {sname}"
            stage_counts[key] = stage_counts.get(key, 0) + 1

        next_url = data.get('next')
        time.sleep(0.3)

    save(all_matches, f"{timestamp}_matches_sample_{page}_pages.json")

    # ------------------------------------------------------------------
    # 4. Stage distribution summary
    # ------------------------------------------------------------------
    print(f"\n[4/4] Stage distribution across {len(all_matches)} sampled matches:")
    if stage_counts:
        for label, count in sorted(stage_counts.items(), key=lambda x: -x[1]):
            print(f"      {count:>4}  candidates  in  {label}")
    else:
        print("      No stage data found in the sampled matches.")

    # ------------------------------------------------------------------
    # Summary file — quick reference for updating TARGET_STAGE_IDS
    # ------------------------------------------------------------------
    summary = {
        "timestamp": timestamp,
        "job_id": JOB_ID,
        "pages_sampled": page,
        "total_matches_sampled": len(all_matches),
        "stage_distribution": stage_counts,
        "stage_id_to_name": {str(k): v for k, v in stage_ids.items()},
        "note": (
            "Update TARGET_STAGE_IDS in core/processing/manatal_fetcher.py "
            "to the ID(s) of the stage(s) you want to process."
        ),
    }
    save(summary, f"{timestamp}_summary.json")

    print("\n" + "=" * 60)
    print("  ✅ Done. Check Manatal_test_api/ for all saved files.")
    print("=" * 60)


if __name__ == "__main__":
    main()
