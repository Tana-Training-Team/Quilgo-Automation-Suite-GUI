# v2/core/processing/api_pusher.py

import requests
import json
import time
import sys
from tqdm import tqdm
import pandas as pd

def execute_api_push_safely(all_candidates_df, all_profiles_raw, api_key):
    """Executes the live API push for ALL processed candidates."""
    print("\n" + "="*80)
    print("🚀 COMMENCING SAFE BATCH PUSH TO MANATAL API")
    print("="*80)
    if all_candidates_df is None or all_candidates_df.empty:
        print("✔ No candidates were processed. Live push concluded.")
        return

    candidate_profiles_map = {c['id']: c for c in all_profiles_raw}
    print(f"[INFO] Created a profile map for {len(candidate_profiles_map)} candidates to ensure data integrity.")
    
    push_headers = {
        "accept": "application/json",
        "Authorization": f"Token {api_key}",
        "Content-Type": "application/json"
    }
    success_count, fail_count = 0, 0

    for _, row in tqdm(all_candidates_df.iterrows(), total=len(all_candidates_df), desc="Pushing to Manatal", file=sys.stdout, dynamic_ncols=True):
        candidate_id = row['id']
        match_pk = row.get('match_pk')
        name = row['full_name']
        
        try:
            # Update Custom Fields
            scores_payload_str = row.get('scores_to_update', '{}')
            if scores_payload_str and scores_payload_str != '{}':
                initial_profile = candidate_profiles_map.get(candidate_id)
                if not initial_profile:
                    print(f"  ❌ FAILED: Could not find initial profile for candidate ID {candidate_id}. Skipping.")
                    fail_count += 1
                    continue
                
                existing_custom_fields = initial_profile.get('custom_fields', {}) or {}
                new_scores_data = json.loads(scores_payload_str)
                existing_custom_fields.update(new_scores_data)
                
                final_merged_payload = {"custom_fields": existing_custom_fields}
                url = f"https://api.manatal.com/open/v3/candidates/{candidate_id}/"
                response = requests.patch(url, headers=push_headers, json=final_merged_payload, timeout=30)
                response.raise_for_status()

            # Post Summary Note
            note_html_content = row.get('summary_note_html') # Assuming this column will be created
            if pd.notna(match_pk) and note_html_content:
                note_payload = {"info": note_html_content}
                url = f"https://api.manatal.com/open/v3/matches/{int(match_pk)}/notes/"
                response = requests.post(url, headers=push_headers, json=note_payload, timeout=30)
                response.raise_for_status()

            success_count += 1
            time.sleep(0.5) # Rate limiting
        except requests.exceptions.RequestException as e:
            print(f"  ❌ API Push FAILED for {name}. Error: {e.response.text if e.response else e}")
            fail_count += 1
            continue

    print("\n" + "#"*80)
    print("### BATCH PUSH COMPLETE ###")
    print(f"Successfully processed {success_count} candidates.")
    if fail_count > 0:
        print(f"Encountered errors for {fail_count} candidates. Please review the log above.")
    print("Please verify the updates in the Manatal UI.\n" + "#"*80)