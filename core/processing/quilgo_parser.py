# v2/core/processing/quilgo_parser.py

import pandas as pd

# --- Quilgo Test Configurations ---
# Each test name is unique. Tests shared between tech and non-tech pipelines list
# all applicable roles together; the per-candidate category filter in the evaluator
# ensures a candidate is only scored against roles from their own pipeline.
MASTER_TEST_CONFIG = {

    # ── QA Engineer (tech only) ───────────────────────────────
    'APIs & Postman':    {'slug': 'apis',           'roles': ['QA Engineer'],                               'type': 'Optional'},
    'JavaScript':        {'slug': 'javascript',     'roles': ['QA Engineer'],                               'type': 'Optional'},
    'Typescript':        {'slug': 'typescrpt',      'roles': ['QA Engineer'],                               'type': 'Optional'},
    'Java':              {'slug': 'java',            'roles': ['QA Engineer'],                               'type': 'Optional'},
    'Python: General':   {'slug': 'pythongeneral',  'roles': ['QA Engineer'],                               'type': 'Optional'},
    'Selenium':          {'slug': 'selenium',       'roles': ['QA Engineer'],                               'type': 'Optional'},
    'Cypress':           {'slug': 'cypress',        'roles': ['QA Engineer'],                               'type': 'Optional'},

    # ── Data Analyst (tech) + None-Tech (non-tech) ────────────
    'Excel':             {'slug': 'excel',          'roles': ['Data Analyst', 'None-Tech'],                 'type': 'Optional'},
    'SQL':               {'slug': 'sql',            'roles': ['Data Analyst', 'None-Tech'],                 'type': 'Optional'},
    'Python: Data':      {'slug': 'pythondata',     'roles': ['Data Analyst', 'Data Science', 'None-Tech'], 'type': 'Optional'},
    'Data Viz: Tableau': {'slug': 'dataviztableau', 'roles': ['Data Analyst', 'None-Tech'],                 'type': 'Optional'},
    'Data Viz: PowerBI': {'slug': 'datavizpowerbi', 'roles': ['Data Analyst', 'None-Tech'],                 'type': 'Optional'},
    'Data Viz: Looker':  {'slug': 'datavizlooker',  'roles': ['Data Analyst', 'None-Tech'],                 'type': 'Optional'},

    # ── Data Science (tech) + None-Tech (non-tech) ────────────
    'Statistics':        {'slug': 'statistics',    'roles': ['Data Science', 'None-Tech'],                  'type': 'Optional'},
    'Machine Learning':  {'slug': 'machinelearning','roles': ['Data Science'],                              'type': 'Optional'},

    # ── Tech Support Engineering (tech) + None-Tech (non-tech) ─
    'Networking':        {'slug': 'networking',    'roles': ['Tech Support Engineering', 'None-Tech'],      'type': 'Optional'},
    'Logs & Errors':     {'slug': 'logserrors',    'roles': ['Tech Support Engineering'],                   'type': 'Optional'},
    'OS Commands: Linux':{'slug': 'oslinux',       'roles': ['Tech Support Engineering'],                   'type': 'Optional'},
    'OS Commands: Windows':{'slug': 'oswindows',   'roles': ['Tech Support Engineering'],                   'type': 'Optional'},

    # ── UI/UX (tech) + None-Tech (non-tech) ───────────────────
    'Figma':             {'slug': 'figma',          'roles': ['UI/UX', 'None-Tech'],                        'type': 'Optional'},
    'Adobe XD':          {'slug': 'adobexd',        'roles': ['UI/UX', 'None-Tech'],                        'type': 'Optional'},
    'Sketch':            {'slug': 'sketch',         'roles': ['UI/UX', 'None-Tech'],                        'type': 'Optional'},

    # ── DevOps / SRE (tech only) ──────────────────────────────
    'Git & CI/CD':       {'slug': 'git',            'roles': ['DevOps / SRE'],                              'type': 'Optional'},
    'AWS':               {'slug': 'aws',            'roles': ['DevOps / SRE'],                              'type': 'Optional'},
    'Azure':             {'slug': 'azure',          'roles': ['DevOps / SRE'],                              'type': 'Optional'},
    'Docker':            {'slug': 'docker',         'roles': ['DevOps / SRE'],                              'type': 'Optional'},
    'Kubernetes':        {'slug': 'kubernetes',     'roles': ['DevOps / SRE'],                              'type': 'Optional'},

}


TEST_NAME_ALIASES = {
    'Tableau': 'Data Viz: Tableau', 
    'Power BI': 'Data Viz: PowerBI', 
    'Looker & LookML': 'Data Viz: Looker', 
    'OS Commands Linux': 'OS Commands: Linux', 
    'OS Commands Windows': 'OS Commands: Windows', 
    'Error Logs': 'Logs & Errors', 
    'Microsoft Azure': 'Azure', 
    'Git & CI CD': 'Git & CI/CD'
}
SLUG_MAPPING = {test_name: details['slug'] for test_name, details in MASTER_TEST_CONFIG.items()}
ROLE_TO_TEST_MAPPING = {}
for test_name, details in MASTER_TEST_CONFIG.items():
    for role in details['roles']:
        ROLE_TO_TEST_MAPPING.setdefault(role, []).append(test_name)
# Maps internal MASTER_TEST_CONFIG key → actual quiz name shown in the Quilgo sidebar.
# Only entries where the two names differ are listed; all others are identical.
INTERNAL_TO_QUILGO_SIDEBAR_NAME = {
    'Data Viz: Tableau': 'Tableau',
    'Data Viz: PowerBI': 'Power BI',
    'Data Viz: Looker':  'Looker & LookML',
    'Logs & Errors':     'Error Logs',
    'Azure':             'Microsoft Azure',
}

ROLE_TO_DROPDOWN_OPTION_MAP = {
    'QA Engineer':              'Quality Assurance (QA) Engineer',
    'Data Analyst':             'Data Analyst',
    'Data Science':             'Data Science',
    'Tech Support Engineering': 'Tech Support Engineering',
    'UI/UX':                    'UI/UX',
    'DevOps / SRE':             'DevOps / Site Reliability Engineer',
    'None-Tech':                'Non-Tech',
}
# Explicit mapping — tech roles come from Manatal job 2619874, non-tech from job 3635455.
# Must stay in sync with JOB_CONFIGS in manatal_fetcher.py.
ROLE_TO_CATEGORY_MAPPING = {
    'QA Engineer':              'tech',
    'Data Analyst':             'tech',
    'Data Science':             'tech',
    'Tech Support Engineering': 'tech',
    'UI/UX':                    'tech',
    'DevOps / SRE':             'tech',
    'None-Tech':                'non-tech',
}

# --- DEFINITIVE FIX: Robust Column Name Mapping ---
# This dictionary maps potential messy column names (after being lowercased) 
# from the CSV to the clean, predictable names we will use internally.
COLUMN_MAPPING = {
    'email': 'email',
    'score': 'score',
    'trust score': 'trust_score',
    'swithed to another tab / window': 'tab_switches', # Handles the "Swithed" typo
    'switched to another tab / window': 'tab_switches',
    'face presence': 'face_presence',
    'camera tracking enabled': 'camera_tracking_enabled', # For future use
    'submitted (utc)': 'submitted_utc', # Submission timestamp for date filtering
    # Add 'copy-paste detected': 'copy_paste' here when the column is available
}


def ingest_and_analyze_quilgo_data(manatal_df, quilgo_source_dir):
    """Finds, processes, and analyzes Quilgo data from the specified source directory."""
    print("\n" + "="*60)
    print("📊 Starting Quilgo Data Processing and Analysis")
    print("="*60)
    print(f"\n[LOG] Scanning for Quilgo CSVs in: {quilgo_source_dir}")
    all_dfs = []
    for filename in quilgo_source_dir.glob('*.csv'):
        if "comparison" not in filename.name.lower():
            try:
                df = pd.read_csv(filename, low_memory=False)
                df['test_name'] = filename.stem
                all_dfs.append(df)
            except Exception as e:
                print(f"  ▲ Could not read or process '{filename.name}'. Error: {e}")
    
    if not all_dfs:
        print("❌ No valid Quilgo CSV files were found to process.")
        return None, None
        
    quilgo_df = pd.concat(all_dfs, ignore_index=True)
    print(f"\n✔ Successfully consolidated {len(all_dfs)} CSVs into a DataFrame with {len(quilgo_df)} total records.")
    
    # --- Step 1: Normalize all column names to lowercase ---
    quilgo_df.columns = [str(col).lower().strip() for col in quilgo_df.columns]
    
    # --- Step 2: Use the mapping to rename columns to our internal standard ---
    quilgo_df.rename(columns=COLUMN_MAPPING, inplace=True)
    
    # Standardize data content
    quilgo_df['email'] = quilgo_df['email'].astype(str).str.lower().str.strip()
    quilgo_df['test_name'] = quilgo_df['test_name'].replace(TEST_NAME_ALIASES)
    print("  - Standardized 'email', 'test_name', and all column headers via robust mapping.")

    # Parse submitted_utc into a proper datetime column.
    # errors='coerce' turns blanks/unparseable values into NaT (not a crash).
    # utc=True normalises the timezone so comparisons are always safe.
    if 'submitted_utc' in quilgo_df.columns:
        quilgo_df['submitted_utc'] = pd.to_datetime(
            quilgo_df['submitted_utc'], format='mixed', errors='coerce', utc=True
        )

    integrity_df = analyze_submission_integrity(quilgo_df)
    
    return quilgo_df, integrity_df

def analyze_submission_integrity(df):
    """Performs a robust, submission-level integrity analysis using the NEW tiered logic."""
    print("\n[INFO] Performing submission-level integrity analysis...")
    
    # Define the columns we need for our analysis based on our clean internal names
    required_cols = ['email', 'test_name', 'trust_score', 'tab_switches', 'face_presence']
    
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        print(f"  ▲ WARNING: The following required integrity columns were not found: {missing_cols}.")
        print("  Skipping integrity analysis. Please check your Quilgo export format and COLUMN_MAPPING.")
        return pd.DataFrame() # Return an empty DataFrame but do not crash

    integrity_df = df[required_cols].copy()
    
    # --- NEW: Tiered Tab Switching Logic ---
    integrity_df['switched_num'] = pd.to_numeric(integrity_df['tab_switches'], errors='coerce').fillna(0)
    integrity_df['face_presence_num'] = pd.to_numeric(integrity_df['face_presence'].astype(str).str.replace('%', ''), errors='coerce').fillna(100)
    
    # Create boolean flags for each condition
    integrity_df['flag_low_trust'] = integrity_df['trust_score'] != 'High'
    integrity_df['flag_low_face'] = integrity_df['face_presence_num'] < 15 # New 15% threshold
    integrity_df['flag_manual_review_switch'] = integrity_df['switched_num'].between(1, 4) # 3 or 4 switches
    integrity_df['flag_auto_fail_switch'] = integrity_df['switched_num'] >= 5 # 5+ switches
    
    # Placeholder for future triggers from your CSV
    # if 'camera_tracking_enabled' in df.columns:
    #     integrity_df['flag_camera_off'] = integrity_df['camera_tracking_enabled'] == 'No' # Example logic
    # else:
    integrity_df['flag_camera_off'] = False # Default to False if column doesn't exist

    # if 'copy_paste' in df.columns:
    #     integrity_df['flag_copy_paste'] = integrity_df['copy_paste'] > 0
    # else:
    #     integrity_df['flag_copy_paste'] = False
        
    # --- Combine flags to determine final status ---
    # A test is flagged for manual review if it meets any of the manual review triggers
    is_manual_review = (
        integrity_df['flag_low_trust'] |
        integrity_df['flag_low_face'] |
        integrity_df['flag_manual_review_switch'] |
        integrity_df['flag_camera_off']
        # | integrity_df['flag_copy_paste'] # Uncomment when ready
    )
    
    # A test is an auto-fail only if it meets the auto-fail switch condition
    is_auto_fail = integrity_df['flag_auto_fail_switch']
    
    # Select only the rows that have at least one flag
    flagged_df = integrity_df[is_manual_review | is_auto_fail].copy()

    if flagged_df.empty:
        print("✔ Integrity analysis complete. No submission-level integrity flags found.")
        return pd.DataFrame()
        
    def get_issue_types(row):
        issues = []
        # List the specific reasons for the flag
        if row['flag_auto_fail_switch']: issues.append(f"Excessive Tab Switches ({int(row['switched_num'])})")
        if row['flag_manual_review_switch']: issues.append(f"Suspicious Tab Switches ({int(row['switched_num'])})")
        if row['flag_low_trust']: issues.append(f"Trust Score: {row['trust_score']}")
        if row['flag_low_face']: issues.append(f"Face Presence ({int(row['face_presence_num'])}%)")
        if row['flag_camera_off']: issues.append("Camera Off Detected")
        # if row['flag_copy_paste']: issues.append("Copy-Paste Detected")
        return ", ".join(issues)

    flagged_df['Issue_Types'] = flagged_df.apply(get_issue_types, axis=1)
    
    # Keep the flag columns for the evaluator to use
    final_cols = ['email', 'test_name', 'Issue_Types', 'flag_auto_fail_switch']
    report_df = flagged_df[final_cols]
    
    print(f"✔ Integrity analysis complete. Found {len(report_df)} tests with submission-level flags.")
    return report_df
