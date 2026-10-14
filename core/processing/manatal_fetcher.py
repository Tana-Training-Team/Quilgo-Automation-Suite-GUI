# v2/core/processing/manatal_fetcher.py

import requests
import pandas as pd
import time
import sys
from tqdm import tqdm
from .http_retry import request_with_retry, REQUEST_DELAY_SECONDS

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


class ManatalFetchIncomplete(Exception):
    """
    Raised when one or more candidates could not be fetched even after retries.
    A partial candidate list must never reach the dashboard or the push — the
    missing fellows would silently get no note, no scores and no stage move.
    """


def _fetch_matches_for_job(job_id, target_stage_ids, job_category, headers):
    """Fetches all active matches in target stages for a single job."""
    matches = []
    next_page_url = f"{BASE_URL}/jobs/{job_id}/matches/?page_size=100"
    with tqdm(desc=f"  [{job_category}] Fetching Match Pages", unit="page", file=sys.stdout, dynamic_ncols=True) as pbar:
        while next_page_url:
            try:
                response = request_with_retry('GET', next_page_url, headers=headers, timeout=30)
            except requests.exceptions.RequestException as e:
                # Stopping here would leave out every candidate on the remaining
                # pages, so abort the whole fetch rather than continue short.
                raise ManatalFetchIncomplete(
                    f"Could not fetch match pages for {job_category} job {job_id} "
                    f"after retries: {e}"
                ) from e
            data = response.json()
            for match in data.get('results', []):
                if match.get('is_active') and match.get('stage', {}).get('id') in target_stage_ids:
                    match['_job_category'] = job_category
                    match['_job_id'] = job_id
                    matches.append(match)
            next_page_url = data.get('next')
            pbar.update(1)
            time.sleep(REQUEST_DELAY_SECONDS)
    return matches


def fetch_manatal_profiles(api_key):
    """
    Fetches candidates from both tech and non-tech jobs, tagged by job_category.
    Each profile carries job_category ('tech' or 'non-tech') and job_id so that
    downstream stage transitions can be applied correctly per pipeline.

    Raises ManatalFetchIncomplete if any candidate in the target stages still
    cannot be fetched after retries — the run must not continue with a partial list.
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
    failed_candidates = []   # (candidate_id, error) for anything still failing after retries
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
            response = request_with_retry('GET', candidate_url, headers=headers, timeout=15)
            profile_data.update(response.json())

            # Set pipeline identifiers after the API response so they are never overwritten
            profile_data['match_pk'] = match_pk
            profile_data['job_id'] = job_id
            profile_data['job_category'] = job_category

            # Existing notes
            notes_url = f"{BASE_URL}/matches/{match_pk}/notes/"
            response = request_with_retry('GET', notes_url, headers=headers, timeout=15)
            notes = response.json()
            if notes:
                profile_data['existing_note_id'] = notes[0].get('id')
                profile_data['existing_note_content'] = notes[0].get('info', 'NO_CONTENT_KEY')
            else:
                profile_data['existing_note_id'] = None
                profile_data['existing_note_content'] = "NO_NOTE_FOUND"

            all_candidate_profiles.append(profile_data)
        except requests.exceptions.RequestException as e:
            print(f"  ▲ Could not fetch full profile for candidate {candidate_id} after retries. Error: {e}")
            failed_candidates.append((candidate_id, e))
        time.sleep(REQUEST_DELAY_SECONDS)

    print("\n" + "="*60)
    if failed_candidates:
        print(f"❌ Manatal fetch INCOMPLETE: {len(failed_candidates)} of {total_found} candidate(s) "
              f"could not be fetched after retries:")
        for cid, err in failed_candidates:
            print(f"  • candidate {cid}: {err}")
        raise ManatalFetchIncomplete(
            f"{len(failed_candidates)} of {total_found} candidate(s) could not be fetched "
            f"from Manatal. Stopping so no fellow is silently left out — please re-run Part 2."
        )
    print(f"✔ Manatal Data Fetching Complete. Successfully Built Full Profiles: {len(all_candidate_profiles)} of {total_found}")
    if not all_candidate_profiles:
        return pd.DataFrame(), []

    manatal_profiles_df = pd.json_normalize(all_candidate_profiles)
    return manatal_profiles_df, all_candidate_profiles
