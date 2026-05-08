# v2/core/processor.py
# This is the main orchestrator for the backend processing logic.

import traceback
import json
import pandas as pd
from app import config # Import the main app config for paths

# Import from our new, refactored modules
from .processing.manatal_fetcher import fetch_manatal_profiles
from .processing.quilgo_parser import ingest_and_analyze_quilgo_data
from .processing.candidate_evaluator import evaluate_and_triage_candidates
from .processing.file_helpers import get_master_folder, create_final_audit_backups
from .processing.api_pusher import execute_api_push_safely

# This dictionary will hold data between runs to pass to the API push function
_cached_data_for_api_push = {
    "all_candidates_df": None,
    "all_profiles_raw": None,
    "integrity_df": None,
}

def _run_common_processing_steps(manatal_df, all_manatal_profiles_raw, project_root, get_manual_review_decision, start_date=None, end_date=None):
    """
    Contains the shared logic from Step 3 onwards.

    Args:
        start_date (pd.Timestamp | None): Optional start of the submission date filter.
        end_date   (pd.Timestamp | None): Optional end of the submission date filter.
                                          Defaults to today when start_date is set but end_date is not.

    Returns:
        tuple: (list of final processed candidate data, boolean for success)
    """
    # Step 3: Process Quilgo Data
    print("\n--- Step 3: Processing Quilgo Data ---")
    latest_run_dir = get_master_folder(project_root)
    if not latest_run_dir: return [], False

    quilgo_df, integrity_df = ingest_and_analyze_quilgo_data(manatal_df, latest_run_dir)
    if quilgo_df is None or quilgo_df.empty:
        print("❌ No Quilgo data processed. Stopping.")
        return [], False

    # Step 4: Evaluate Candidates
    # --- MODIFICATION: Capture the full list of candidates from the evaluator ---
    print("\n--- Step 4: Evaluating and Triaging Candidates ---")
    final_processed_candidates, approved_df, rejected_df = evaluate_and_triage_candidates(
        manatal_df, quilgo_df, integrity_df, get_manual_review_decision,
        start_date=start_date, end_date=end_date)

    # Step 5: Create Backups
    print("\n--- Step 5: Creating Final Audit Logs and Backups ---")
    create_final_audit_backups(approved_df, rejected_df, config.DOWNLOADS_DIR)
    
    # Cache the final processed dataframes for the API push step
    all_candidates_to_process = pd.concat([approved_df, rejected_df], ignore_index=True)
    _cached_data_for_api_push['all_candidates_df'] = all_candidates_to_process
    _cached_data_for_api_push['all_profiles_raw'] = all_manatal_profiles_raw
    _cached_data_for_api_push['integrity_df'] = integrity_df
    
    # --- MODIFICATION: Return the full candidate data list along with the success flag ---
    return final_processed_candidates, True

def run_or_rerun_processing(use_cache, api_key, project_root, get_manual_review_decision, start_date=None, end_date=None):
    """
    A single entry point that orchestrates the entire Python processing workflow,
    either from scratch or from a cache.

    Args:
        start_date (pd.Timestamp | None): Optional start of the submission date filter.
        end_date   (pd.Timestamp | None): Optional end of the submission date filter.

    Returns:
        tuple: (list of final processed candidate data, boolean for success)
    """
    try:
        if use_cache:
            # Step 2 (from cache)
            print("\n--- Step 2 (Cache): Loading Manatal Profiles from Cache ---")
            if not config.MANATAL_CACHE_FILE.exists():
                print(f"❌ ERROR: Manatal cache file not found at {config.MANATAL_CACHE_FILE}")
                return [], False
            with open(config.MANATAL_CACHE_FILE, 'r') as f:
                all_manatal_profiles_raw = json.load(f)
            manatal_df = pd.json_normalize(all_manatal_profiles_raw)
            print(f"✔ Successfully loaded {len(manatal_df)} profiles from cache.")
        else:
            # Step 2 (from API)
            print("\n--- Step 2: Fetching Manatal Profiles ---")
            manatal_df, all_manatal_profiles_raw = fetch_manatal_profiles(api_key)
            if manatal_df is None or manatal_df.empty:
                print("❌ No Manatal profiles found. Stopping.")
                return [], False

            print("\n--- Step 2 (Cache): Saving Manatal profiles to cache for re-runs ---")
            config.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
            with open(config.MANATAL_CACHE_FILE, 'w') as f:
                json.dump(all_manatal_profiles_raw, f, indent=4)
            print(f"✔ Cache saved to {config.MANATAL_CACHE_FILE.name}")

        # Run the subsequent common processing steps and return their result
        return _run_common_processing_steps(
            manatal_df, all_manatal_profiles_raw, project_root,
            get_manual_review_decision, start_date=start_date, end_date=end_date
        )

    except Exception as e:
        print(f"\n❌ A critical error occurred during Python processing: {e}")
        traceback.print_exc()
        return [], False

def refresh_push_cache_from_results(final_results):
    """
    Rebuild `_cached_data_for_api_push['all_candidates_df']` from the edited
    `final_results` list that the dashboard owns.

    Why this exists:
        After Part 2, `_cached_data_for_api_push` holds a DataFrame assembled
        from the evaluator's output. Any edit the reviewer makes on the Final
        Review dashboard (score changes, manual Approve/Reject, Pending
        resolutions) mutates `st.session_state.final_results` — but that
        change NEVER made it back into the push cache, so the API push would
        ship stale data.

        Each candidate dict carries `original_row` (the pivoted Manatal row
        enriched with summary notes + scores_to_update JSON) which is exactly
        the shape `execute_api_push_safely` consumes, so we can rebuild the
        DataFrame directly from those rows. `_reevaluate` in the UI layer
        keeps `original_row["scores_to_update"]` in sync on every save.

        `all_profiles_raw` is untouched — it's Manatal's raw profile payload,
        independent of dashboard edits.

    Returns the number of rows placed in the cache, for logging.
    """
    if not final_results:
        # Nothing to refresh — leave the cache alone.
        return 0
    rows = []
    for candidate in final_results:
        row = candidate.get('original_row')
        if not row: continue
        # Defensive copy so the cache isn't sharing mutable state with the UI.
        rows.append(dict(row))
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    _cached_data_for_api_push['all_candidates_df'] = df
    print(f"[push cache] Refreshed from dashboard edits: {len(df)} candidate row(s).")
    return len(df)


def trigger_api_push(api_key, final_results=None):
    """
    The final, separate step to push the cached results to the Manatal API.
    If final_results is provided (from the dashboard), the cache is refreshed
    first so any edits made after Part 2 (approvals, score changes) are included.
    """
    try:
        if final_results:
            refresh_push_cache_from_results(final_results)

        all_candidates_df = _cached_data_for_api_push.get('all_candidates_df')
        all_profiles_raw = _cached_data_for_api_push.get('all_profiles_raw')

        if all_candidates_df is None or all_profiles_raw is None:
            print("❌ ERROR: No processed candidate data found in cache. Please run the processor first.")
            return False
        
        execute_api_push_safely(all_candidates_df, all_profiles_raw, api_key)
        
        return True
    except Exception as e:
        print(f"\n❌ A critical error occurred during the API Push: {e}")
        traceback.print_exc()
        return False

def get_cached_integrity_df():
    """Returns the integrity_df stored during the last processing run, or an empty DataFrame."""
    result = _cached_data_for_api_push.get('integrity_df')
    return result if result is not None else pd.DataFrame()


# --- MODIFICATION: The old entry points are now combined into run_or_rerun_processing ---
# This makes the file cleaner and removes redundant code. The old run_all_processing and
# rerun_processing_from_cache functions are no longer needed as separate top-level calls.