# v2/app/config.py

from pathlib import Path

# --- Core Paths ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / 'gui_config.ini'
PLAYWRIGHT_DIR = PROJECT_ROOT / 'playwright_downloader'
ASSETS_DIR = PROJECT_ROOT / 'assets'
QUILGO_RUNS_DIR  = PROJECT_ROOT / 'Quilgo'
QUILGO_MASTER_DIR = PROJECT_ROOT / 'Quilgo' / 'master'
QUILGO_BACKUP_DIR = PROJECT_ROOT / 'Quilgo' / 'backup'
DOWNLOADS_DIR = PROJECT_ROOT / 'downloads'

# --- Cache file for re-running the processor ---
MANATAL_CACHE_FILE = PROJECT_ROOT / 'downloads' / 'manatal_profiles_cache.json'

# --- Quiz selection file: Python writes this, Playwright reads it ---
# Contains the list of quiz names the user selected in the GUI.
SELECTED_QUIZZES_FILE = PROJECT_ROOT / 'selected_quizzes.json'

# --- Styling Theme ---
THEME = {
    "bg_color": "#F0F8FF",
    "text_color": "#002244",
    "title_color": "#003366",
    "button_bg": "#007BFF",
    "button_fg": "white",
    "button_hover_bg": "#0056b3",
    "secondary_button_bg": "#E7F1FF",
    "secondary_button_fg": "#003366",
    "log_bg": "#001a33",
    "log_fg": "#E0E0E0",
    "success_color": "#28a745",
    "danger_color": "#dc3545",
    "warning_color": "#ffc107",
    "info_color": "#17a2b8",
    # --- NEW: Status Colors for Review UI ---
    "status_manual_review": "#ffc107", # Warning Yellow
    "status_fail": "#dc3545",          # Danger Red
    "status_qualified": "#28a745",     # Success Green
    "status_borderline": "#17a2b8",    # Info Blue
}

# --- Font Definitions ---
TITLE_FONT = ("Helvetica", 14, "bold")
BODY_FONT = ("Helvetica", 10)
LOG_FONT = ("Courier New", 10)