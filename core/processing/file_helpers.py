# v2/core/processing/file_helpers.py
# Contains utility functions for file and folder operations.

import json
import shutil
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Master / backup folder management
# ---------------------------------------------------------------------------

def prepare_fresh_master(project_root: Path) -> None:
    """Clears master/ so Playwright writes into a clean directory each run."""
    master = project_root / 'Quilgo' / 'master'
    if master.exists():
        shutil.rmtree(master)
    master.mkdir(parents=True, exist_ok=True)
    print(f"✔ Created fresh Quilgo/master/ for this run.")


def upsert_master_into_backup(project_root: Path) -> dict:
    """
    Merges master/ CSVs into backup/ after each Part 1 run.

    Per quiz file:
      - Emails new to backup  → appended as new rows.
      - Emails already in backup → their row is replaced with fresh master data.
      - Emails in backup but absent from master → kept as-is.

    Returns {filename: {"new": int, "updated": int}} so write_manifest can
    log both types of change separately in the backup manifest.
    """
    master = project_root / 'Quilgo' / 'master'
    backup = project_root / 'Quilgo' / 'backup'
    backup.mkdir(parents=True, exist_ok=True)
    stats_by_file: dict = {}

    for master_csv in sorted(master.glob('*.csv')):
        name = master_csv.name
        backup_csv = backup / name

        try:
            master_df = pd.read_csv(master_csv, low_memory=False)
            email_col = next((c for c in master_df.columns if c.lower() == 'email'), None)

            if not email_col:
                master_df.to_csv(backup_csv, index=False, encoding='utf-8')
                stats_by_file[name] = {'new': len(master_df), 'updated': 0}
                print(f"  ✔ '{name}' → copied to backup (no email column, {len(master_df)} rows)")
                continue

            master_df['_key'] = master_df[email_col].astype(str).str.lower().str.strip()

            if backup_csv.exists():
                backup_df = pd.read_csv(backup_csv, low_memory=False)
                backup_email_col = next((c for c in backup_df.columns if c.lower() == 'email'), email_col)
                backup_df['_key'] = backup_df[backup_email_col].astype(str).str.lower().str.strip()

                backup_keys  = set(backup_df['_key'])
                master_keys  = set(master_df['_key'])
                new_count     = int((~master_df['_key'].isin(backup_keys)).sum())
                updated_count = int(master_df['_key'].isin(backup_keys).sum())

                untouched  = backup_df[~backup_df['_key'].isin(master_keys)]
                merged_df  = pd.concat([untouched, master_df], ignore_index=True)
            else:
                new_count     = len(master_df)
                updated_count = 0
                merged_df     = master_df.copy()

            merged_df = merged_df.drop(columns=['_key'], errors='ignore')
            merged_df.to_csv(backup_csv, index=False, encoding='utf-8')
            stats_by_file[name] = {'new': new_count, 'updated': updated_count}
            print(f"  ✔ '{name}' → backup ({new_count} new, {updated_count} updated, {len(merged_df)} total)")

        except Exception as e:
            print(f"  ▲ Could not upsert '{name}': {e}")
            stats_by_file[name] = {'new': 0, 'updated': 0}

    return stats_by_file


def get_master_folder(project_root: Path) -> Path | None:
    """Returns Quilgo/master/ — the single source of truth for quiz CSVs."""
    master = project_root / 'Quilgo' / 'master'
    if not master.exists():
        print("❌ ERROR: Quilgo/master/ not found. Has Part 1 run yet?")
        return None
    csv_files = list(master.glob('*.csv'))
    if not csv_files:
        print("❌ ERROR: Quilgo/master/ exists but contains no CSV files.")
        return None
    print(f"✔ Using master folder: {master}  ({len(csv_files)} CSV file(s))")
    return master


def _count_rows(csv_path: Path) -> int:
    try:
        with open(csv_path, 'r', encoding='utf-8', errors='replace') as f:
            return max(0, sum(1 for _ in f) - 1)
    except Exception:
        return 0


def write_manifest(project_root: Path, stats_by_file: dict | None = None) -> None:
    """
    Writes two distinct manifests after each Part 1 run.

    master/manifest.json
        Tracks this run's fresh download only.
        'created' = timestamp of this run (master is always a new download).
        'row_count' = rows in master/ for this run.

    backup/manifest.json
        Tracks the cumulative growing dataset.
        'created' = when this quiz CSV first appeared in backup (never changes).
        'last_updated' = when backup was last modified for this file.
        'row_count' = total rows now in backup/ (grows across runs).
        'new_rows_this_run' / 'updated_rows_this_run' = change log per run.

    stats_by_file: {filename: {"new": int, "updated": int}} from upsert_master_into_backup.
    """
    master = project_root / 'Quilgo' / 'master'
    backup = project_root / 'Quilgo' / 'backup'
    master_manifest_path = master / 'manifest.json'
    backup_manifest_path = backup / 'manifest.json'
    stats = stats_by_file or {}

    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # ── master manifest: snapshot of this run's downloads ────────────────────
    # 'created' is always now — master is a brand-new download every run.
    master_files: dict = {}
    for csv_path in sorted(master.glob('*.csv')):
        name = csv_path.name
        file_stats = stats.get(name, {})
        master_files[name] = {
            'created':              now,
            'row_count':            _count_rows(csv_path),
            'new_rows_to_backup':   file_stats.get('new', 0),
            'updated_rows_in_backup': file_stats.get('updated', 0),
        }

    with open(master_manifest_path, 'w', encoding='utf-8') as f:
        json.dump({
            'last_run':    now,
            'total_files': len(master_files),
            'files':       master_files,
        }, f, indent=2)

    # ── backup manifest: cumulative growing dataset ───────────────────────────
    # Read the existing backup manifest to preserve 'created' dates.
    prev_files: dict = {}
    backup.mkdir(parents=True, exist_ok=True)
    if backup_manifest_path.exists():
        try:
            with open(backup_manifest_path, 'r', encoding='utf-8') as f:
                prev_files = json.load(f).get('files', {})
        except Exception:
            pass

    # Union of all known files: ones downloaded this run + any from previous runs.
    all_names = sorted(set(master_files.keys()) | set(prev_files.keys()))
    backup_files: dict = {}
    for name in all_names:
        prev       = prev_files.get(name, {})
        backup_csv = backup / name
        file_stats = stats.get(name, {})
        touched_this_run = name in master_files

        backup_files[name] = {
            # 'created' is set once when the file first appears in backup.
            'created':                prev.get('created', now),
            # 'last_updated' only advances when this file was actually upserted.
            'last_updated':           now if touched_this_run else prev.get('last_updated', now),
            'row_count':              _count_rows(backup_csv) if backup_csv.exists() else 0,
            'new_rows_this_run':      file_stats.get('new', 0),
            'updated_rows_this_run':  file_stats.get('updated', 0),
        }

    with open(backup_manifest_path, 'w', encoding='utf-8') as f:
        json.dump({
            'last_run':    now,
            'total_files': len(backup_files),
            'files':       backup_files,
        }, f, indent=2)

    total_new     = sum(v['new_rows_this_run']     for v in backup_files.values())
    total_updated = sum(v['updated_rows_this_run'] for v in backup_files.values())
    total_backup  = sum(v['row_count']             for v in backup_files.values())
    print(f"\n✔ Manifests written → master/ and backup/")
    print(f"  This run : {len(master_files)} file(s) downloaded")
    print(f"  Backup   : {len(backup_files)} file(s) | {total_backup} total rows "
          f"| {total_new} new, {total_updated} updated this run")


# ---------------------------------------------------------------------------
# Audit log helpers (unchanged)
# ---------------------------------------------------------------------------

def _save_backup_file(df, title, filename, download_dir, file_format='csv'):
    """Saves a DataFrame to a file (CSV or JSON) and prints a confirmation message."""
    download_dir.mkdir(parents=True, exist_ok=True)
    filepath = download_dir / filename
    try:
        if file_format == 'csv':
            df.to_csv(filepath, index=False, encoding='utf-8')
        elif file_format == 'json':
            records = df.to_dict(orient='records')
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(records, f, indent=4)
        print(f"  ✔ Saved backup '{title}' to: {filepath.name}")
    except Exception as e:
        print(f"  ▲ Could not save backup file '{filename}'. Error: {e}")


def create_final_audit_backups(approved_df, rejected_df, download_dir):
    """Consolidates all decisions into a single audit log and saves it."""
    print("\n" + "="*60)
    print("📋 Generating Final Audit Log & Backups")
    print("="*60)
    if approved_df.empty and rejected_df.empty:
        print("✔ No candidates were processed. No audit log to generate.")
        return

    approved_df_copy = approved_df.copy()
    rejected_df_copy = rejected_df.copy()
    approved_df_copy['final_decision'] = 'APPROVED'
    rejected_df_copy['final_decision'] = 'REJECTED'

    full_audit_df = pd.concat([approved_df_copy, rejected_df_copy], ignore_index=True)
    audit_cols = ['id', 'full_name', 'email', 'final_decision', 'summary_note_md', 'scores_to_update']
    final_audit_cols = [col for col in audit_cols if col in full_audit_df.columns]

    print(f"✔ Created a comprehensive audit log for {len(full_audit_df)} candidates.")

    _save_backup_file(full_audit_df[final_audit_cols], "Full Audit Log (CSV)", "2_full_audit_log.csv", download_dir, file_format='csv')
    _save_backup_file(full_audit_df, "Full Raw Data with Decisions (JSON)", "2_full_audit_log_raw.json", download_dir, file_format='json')
