# v2/core/processing/api_pusher.py

import requests
import json
import time
import sys
from tqdm import tqdm
import pandas as pd

BASE_URL = "https://api.manatal.com/open/v3"

# ---------------------------------------------------------------------------
# TEST MODE GUARD
# When TEST_MODE is True only the emails in TEST_CANDIDATE_EMAILS are pushed.
# Every other candidate is skipped and logged — nothing is written to Manatal.
# Set TEST_MODE = False to push all candidates when you are ready to go live.
#
# These hardcoded values are the fallback defaults. At push time the live
# values are resolved by _load_test_settings(), which checks gui_config.ini
# [push_settings] first and falls back here when nothing is configured.
# ---------------------------------------------------------------------------
TEST_MODE = True

DEFAULT_TEST_EMAILS = {
    "joy.mwagiru+test1@tanatech.io",
    "joy.mwagiru+test2@tanatech.io",
    "joy.mwagiru+test3@tanatech.io",
}
TEST_CANDIDATE_EMAILS = DEFAULT_TEST_EMAILS  # kept for backwards-compat imports
# ---------------------------------------------------------------------------


def _load_test_settings():
    """Return (effective_test_mode, effective_emails) by reading gui_config.ini.

    Falls back to the hardcoded module-level defaults when the ini has no
    [push_settings] section or when the email list stored there is empty.
    """
    import configparser
    try:
        from app import config as _cfg
        cfg_path = _cfg.CONFIG_FILE
    except Exception:
        return TEST_MODE, DEFAULT_TEST_EMAILS

    p = configparser.ConfigParser(interpolation=None)
    p.read(cfg_path)

    if not p.has_section("push_settings"):
        return TEST_MODE, DEFAULT_TEST_EMAILS

    s = p["push_settings"]
    mode = s.getboolean("test_mode", TEST_MODE)
    raw = s.get("test_candidate_emails", "").strip()
    if raw:
        emails = {e.strip().lower() for e in raw.split(",") if e.strip()}
    else:
        emails = DEFAULT_TEST_EMAILS
    return mode, emails

# Stage transitions: job_category → attempt_outcome → target stage ID
# attempt_outcome values: 'passed' | 'attempted_failed' | 'not_attempted'
# None = leave the candidate in their current stage (no PATCH call made)
#
# Tech pipeline (job 2619874), pulled from 1608891 (Review Quilgo Assessments):
#   passed / attempted_failed → 1608892 (Complete Quilgo Assessments)
#   not_attempted             → remain in 1608891 (no move)
#
# Non-tech pipeline (job 3635455), pulled from 1896269 ((NEW) Review Tests):
#   passed           → 1802190 (Passed Tests)
#   attempted_failed → 1896270 ((NEW) Failed Tests)
#   not_attempted    → remain in 1896269 (no move)
STAGE_TRANSITIONS = {
    'tech': {
        'passed': 1608892,
        'attempted_failed': 1608892,
        'not_attempted': None,
    },
    'non-tech': {
        'passed': 1802190,
        'attempted_failed': 1896270,
        'not_attempted': None,
    },
}


def execute_api_push_safely(all_candidates_df, all_profiles_raw, api_key):
    """Executes the live API push for ALL processed candidates."""
    # Resolve test settings fresh at push time so UI changes take effect
    # without restarting the process.
    effective_test_mode, effective_test_emails = _load_test_settings()

    print("\n" + "="*80)
    print("🚀 COMMENCING SAFE BATCH PUSH TO MANATAL API")
    if effective_test_mode:
        print("⚠️  TEST MODE ACTIVE — only whitelisted candidates will be pushed.")
        print(f"   Whitelisted emails: {', '.join(sorted(effective_test_emails))}")
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
    success_count, fail_count, skipped_count = 0, 0, 0
    pushed_candidates = []   # log of every candidate actually sent to Manatal
    skipped_candidates = []  # log of every candidate blocked by TEST_MODE

    for _, row in tqdm(all_candidates_df.iterrows(), total=len(all_candidates_df), desc="Pushing to Manatal", file=sys.stdout, dynamic_ncols=True):
        candidate_id = row['id']
        match_pk = row.get('match_pk')
        job_id = row.get('job_id')
        job_category = row.get('job_category', 'tech')
        attempt_outcome = row.get('attempt_outcome', 'not_attempted')
        name = row['full_name']
        email = str(row.get('email', '')).lower().strip()

        # --- TEST MODE GUARD ---
        if effective_test_mode and email not in effective_test_emails:
            skipped_candidates.append(f"  • {name} ({email})")
            skipped_count += 1
            continue

        try:
            # Step 1: Update custom fields (quiz scores)
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
                url = f"{BASE_URL}/candidates/{candidate_id}/"
                response = requests.patch(url, headers=push_headers, json=final_merged_payload, timeout=30)
                response.raise_for_status()

            # Step 2: Add a new summary note to the match.
            # We always POST (create) — existing notes are never patched or deleted.
            # If a note was already present when the candidate was fetched, it is
            # preserved and the new Quilgo note is added alongside it.
            note_html_content = row.get('summary_note_html')
            if pd.notna(match_pk) and note_html_content:
                existing_note_id = row.get('existing_note_id')
                if pd.notna(existing_note_id) if existing_note_id is not None else False:
                    print(f"  ℹ️  {name}: existing note (ID {int(existing_note_id)}) preserved — adding new Quilgo note alongside it.")
                note_payload = {"info": note_html_content}
                url = f"{BASE_URL}/matches/{int(match_pk)}/notes/"
                response = requests.post(url, headers=push_headers, json=note_payload, timeout=30)
                response.raise_for_status()

            # Step 3: Transition the candidate's pipeline stage.
            # Correct endpoint: PATCH /matches/{match_pk}/  (not the job-scoped URL which is GET-only)
            # Correct payload:  {"stage": {"id": stage_id}}  (stage is an object, not a bare integer)
            target_stage_id = STAGE_TRANSITIONS.get(job_category, {}).get(attempt_outcome)
            if target_stage_id is not None and pd.notna(match_pk):
                stage_payload = {"stage": {"id": target_stage_id}}
                url = f"{BASE_URL}/matches/{int(match_pk)}/"
                response = requests.patch(url, headers=push_headers, json=stage_payload, timeout=30)
                response.raise_for_status()
                print(f"  ✔ Stage updated for {name}: [{job_category}] {attempt_outcome} → stage {target_stage_id}")

            pushed_candidates.append(f"  • {name} ({email}) | {job_category} | {attempt_outcome}")
            success_count += 1
            time.sleep(0.5)
        except requests.exceptions.RequestException as e:
            # e.response is falsy for 4xx/5xx — use 'is not None' to access the body
            if getattr(e, 'response', None) is not None:
                error_detail = f"{e} | API body: {e.response.text}"
            else:
                error_detail = str(e)
            print(f"  ❌ API Push FAILED for {name} ({email}). Error: {error_detail}")
            fail_count += 1
            continue

    # --- Final summary ---
    print("\n" + "#"*80)
    print("### BATCH PUSH COMPLETE ###")

    if pushed_candidates:
        print(f"\n✅ Successfully pushed ({success_count} candidate(s)):")
        for entry in pushed_candidates:
            print(entry)

    if fail_count > 0:
        print(f"\n❌ Failed to push ({fail_count} candidate(s)) — review errors above.")

    if effective_test_mode and skipped_candidates:
        print(f"\n⏭  Skipped — not in test whitelist ({skipped_count} candidate(s)):")
        for entry in skipped_candidates:
            print(entry)
        print("\n  To push all candidates, disable Test Mode in Settings.")

    print("\nPlease verify the updates in the Manatal UI.")
    print("#"*80)
