# v2/core/processing/manatal_fetcher.py

import requests
import pandas as pd
import time
import sys
from tqdm import tqdm

# --- Manatal API Settings ---
BASE_URL = "https://api.manatal.com/open/v3"

# Both job pipelines and the stages from which candidates are pulled.
# Tech: job 2619874, stage 1608891 (Review Quilgo Assessments)
# Non-tech: job 3635455, stage 1896269 ((NEW) Review Tests)
JOB_CONFIGS = {
    'tech': {
        'job_id': 2619874,
        'target_stage_ids': {1608891},
    },
    'non-tech': {
        'job_id': 3635455,
        'target_stage_ids': {1896269},
    },
}


def _fetch_matches_for_job(job_id, target_stage_ids, job_category, headers):
    """Fetches all active matches in target stages for a single job."""
    matches = []
    next_page_url = f"{BASE_URL}/jobs/{job_id}/matches/?page_size=100"
    with tqdm(desc=f"  [{job_category}] Fetching Match Pages", unit="page", file=sys.stdout, dynamic_ncols=True) as pbar:
        while next_page_url:
            try:
                response = requests.get(next_page_url, headers=headers, timeout=30)
                response.raise_for_status()
                data = response.json()
                for match in data.get('results', []):
                    if match.get('is_active') and match.get('stage', {}).get('id') in target_stage_ids:
                        match['_job_category'] = job_category
                        match['_job_id'] = job_id
                        matches.append(match)
                next_page_url = data.get('next')
                pbar.update(1)
                time.sleep(0.2)
            except requests.exceptions.RequestException as e:
                print(f"❌ HTTP Request failed while fetching matches for job {job_id}: {e}")
                break
    return matches


def fetch_manatal_profiles(api_key):
    """
    Fetches candidates from both tech and non-tech jobs, tagged by job_category.
    Each profile carries job_category ('tech' or 'non-tech') and job_id so that
    downstream stage transitions can be applied correctly per pipeline.
    """
    print("🚀 Starting to fetch comprehensive candidate profiles from Manatal API...")
    headers = {"accept": "application/json", "Authorization": f"Token {api_key}"}

    # Phase 1: Fetch all matches from both job pipelines
    print("  Phase 1: Fetching all job matches...")
    all_matches = []
    for job_category, cfg in JOB_CONFIGS.items():
        print(f"\n  → Fetching {job_category} candidates from job {cfg['job_id']}...")
        matches = _fetch_matches_for_job(cfg['job_id'], cfg['target_stage_ids'], job_category, headers)
        print(f"  Found {len(matches)} {job_category} candidate(s) in target stage(s).")
        all_matches.extend(matches)

    total_found = len(all_matches)
    print(f"\n  Total candidates across both pipelines: {total_found}")

    if not all_matches:
        print("  No candidates found in any target stage. Stopping Manatal fetch.")
        return pd.DataFrame(), []

    # Phase 2: Enrich profiles with full candidate details and existing notes
    print("\n  Phase 2: Enriching profiles with details and notes...")
    all_candidate_profiles = []
    for match in tqdm(all_matches, desc="Fetching Candidate Details", unit="candidate", file=sys.stdout, dynamic_ncols=True):
        candidate_id = match.get('candidate')
        match_pk = match.get('id')
        job_category = match.get('_job_category')
        job_id = match.get('_job_id')
        if not candidate_id:
            continue

        profile_data = {}
        try:
            # Candidate details
            candidate_url = f"{BASE_URL}/candidates/{candidate_id}/"
            response = requests.get(candidate_url, headers=headers, timeout=15)
            response.raise_for_status()
            profile_data.update(response.json())

            # Set pipeline identifiers after the API response so they are never overwritten
            profile_data['match_pk'] = match_pk
            profile_data['job_id'] = job_id
            profile_data['job_category'] = job_category

            # Existing notes
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
