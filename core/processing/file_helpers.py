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

def rotate_master_to_backup(project_root: Path) -> None:
    """
    Moves Quilgo/master/ → Quilgo/backup/ before a new download run.
    Backup is always overwritten so only one previous copy is kept.
    A fresh empty master/ is created ready for Playwright to write into.
    """
    master = project_root / 'Quilgo' / 'master'
    backup = project_root / 'Quilgo' / 'backup'

    if master.exists():
        if backup.exists():
            shutil.rmtree(backup)
        shutil.move(str(master), str(backup))
        print(f"✔ Rotated  Quilgo/master → Quilgo/backup")
    else:
        print(f"[INFO] No existing master folder — starting fresh.")

    master.mkdir(parents=True, exist_ok=True)
    print(f"✔ Created fresh  Quilgo/master/  for this run.")


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


def write_manifest(project_root: Path) -> None:
    """
    Writes Quilgo/master/manifest.json recording when each CSV was
    first created and last updated, plus its current row count.

    Creation dates are preserved from the previous backup manifest so the
    date a quiz was first ever downloaded is never lost across rotations.
    """
    master = project_root / 'Quilgo' / 'master'
    backup = project_root / 'Quilgo' / 'backup'
    manifest_path = master / 'manifest.json'
    backup_manifest_path = backup / 'manifest.json'

    # Load the previous manifest (from backup) to keep original creation dates
    prev_files: dict = {}
    if backup_manifest_path.exists():
        try:
            with open(backup_manifest_path, 'r', encoding='utf-8') as f:
                prev_files = json.load(f).get('files', {})
        except Exception:
            pass

    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    files: dict = {}

    for csv_path in sorted(master.glob('*.csv')):
        name = csv_path.name
        prev = prev_files.get(name, {})

        # Count data rows (total lines minus 1 header)
        try:
            with open(csv_path, 'r', encoding='utf-8', errors='replace') as f:
                row_count = max(0, sum(1 for _ in f) - 1)
        except Exception:
            row_count = 0

        prev_count = prev.get('row_count', 0)
        new_rows = max(0, row_count - prev_count)

        files[name] = {
            'created':      prev.get('created', now),   # keep original creation date
            'last_updated': now,
            'row_count':    row_count,
            'new_rows_this_run': new_rows,
        }

    manifest = {
        'last_run': now,
        'total_files': len(files),
        'files': files,
    }

    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)

    print(f"\n✔ Manifest written → Quilgo/master/manifest.json")
    print(f"  {len(files)} file(s) tracked. New rows this run: "
          f"{sum(v['new_rows_this_run'] for v in files.values())}")


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
