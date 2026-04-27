# v2/core/processing/manatal_fetcher.py

import requests
import pandas as pd
import time
import sys
from tqdm import tqdm

# --- Manatal API Settings ---
JOB_ID = 2619874
BASE_URL = "https://api.manatal.com/open/v3"
TARGET_STAGE_IDS = {1608891}

def fetch_manatal_profiles(api_key):
    """
    Fetches all candidates in target stages from the Manatal API and
    enriches them with full profile details and existing notes.
    """
    print("🚀 Starting to fetch comprehensive candidate profiles from Manatal API...")
    headers = {"accept": "application/json", "Authorization": f"Token {api_key}"}
    
    # Phase 1: Fetch all job matches in the target stages
    print("  Phase 1: Fetching all job matches...")
    matches_to_process = []
    next_page_url = f"{BASE_URL}/jobs/{JOB_ID}/matches/?page_size=100"
    with tqdm(desc="Fetching Match Pages", unit="page", file=sys.stdout, dynamic_ncols=True) as pbar:
        while next_page_url:
            try:
                response = requests.get(next_page_url, headers=headers, timeout=30)
                response.raise_for_status()
                data = response.json()
                for match in data.get('results', []):
                    if match.get('is_active') and match.get('stage', {}).get('id') in TARGET_STAGE_IDS:
                        matches_to_process.append(match)
                next_page_url = data.get('next')
                pbar.update(1)
                time.sleep(0.2)
            except requests.exceptions.RequestException as e:
                print(f"❌ HTTP Request failed while fetching matches: {e}")
                break
    
    total_found = len(matches_to_process)
    print(f"  Found {total_found} total active candidates in target stages.")

    if not matches_to_process:
        print("  No candidates found in the target stage. Stopping Manatal fetch.")
        return pd.DataFrame(), []

    # Phase 2: Enrich profiles with full details and notes
    print("\n  Phase 2: Enriching profiles with details and notes...")
    all_candidate_profiles = []
    for match in tqdm(matches_to_process, desc="Fetching Candidate Details", unit="candidate", file=sys.stdout, dynamic_ncols=True):
        candidate_id = match.get('candidate')
        match_pk = match.get('id')
        if not candidate_id: continue
        profile_data = {'match_pk': match_pk}
        try:
            # Get candidate details
            candidate_url = f"{BASE_URL}/candidates/{candidate_id}/"
            response = requests.get(candidate_url, headers=headers, timeout=15)
            response.raise_for_status()
            profile_data.update(response.json())
            
            # Get notes
            notes_url = f"{BASE_URL}/matches/{match_pk}/notes/"
            response = requests.get(notes_url, headers=headers, timeout=15)
            response.raise_for_status()
            notes = response.json()
            if notes:
                profile_data['existing_note_id'] = notes[0].get('id')
                profile_data['existing_note_content'] = notes[0].get('info', 'NO_CONTENT_KEY')
            else:
                profile_data['existing_note_id'] = None
                profile_data['existing_note_content'] = "NO_NOTE_FOUND"
            
            all_candidate_profiles.append(profile_data)
            time.sleep(0.2)
        except requests.exceptions.RequestException as e:
            print(f"  ▲ Could not fetch full profile for candidate {candidate_id}. Error: {e}")
            continue

    print("\n" + "="*60)
    print(f"✔ Manatal Data Fetching Complete. Successfully Built Full Profiles: {len(all_candidate_profiles)}")
    if not all_candidate_profiles:
        return pd.DataFrame(), []

    manatal_profiles_df = pd.json_normalize(all_candidate_profiles)
    return manatal_profiles_df, all_candidate_profiles