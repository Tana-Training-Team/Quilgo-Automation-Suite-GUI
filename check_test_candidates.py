"""
check_test_candidates.py
------------------------
Fetches and prints the live Manatal state for the two test candidates so you
can confirm that automation changes actually took effect.

Run from the project root:
    python check_test_candidates.py

FLOW (efficient — no bulk scanning)
  For each test email:
    1. Resolve candidate ID via Manatal search (tries a few query strategies)
    2. Fetch that one candidate profile directly: GET /candidates/{id}/
    3. For each job pipeline, query matches filtered to that candidate ID:
         GET /jobs/{job_id}/matches/?candidate={id}
    4. Fetch notes for any match found

Total API calls per candidate: ~5-8 regardless of how many candidates exist.
"""

import configparser
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_FILE  = PROJECT_ROOT / "gui_config.ini"
OUTPUT_DIR   = PROJECT_ROOT / "check_output"
BASE_URL     = "https://api.manatal.com/open/v3"

TEST_EMAILS = {
    "joy.mwagiru+test1@tanatech.io",
    "joy.mwagiru+test2@tanatech.io",
    "joy.mwagiru+test3@tanatech.io",
    "joy.mwagiru+test3@tanatech.io"
}

# Both job pipelines — mirrors manatal_fetcher.py and api_pusher.py
JOB_CONFIGS = {
    "tech": {
        "job_id":            2619874,
        "label":             "Tech Pipeline",
        "source_stage_id":   1608891,
        "source_stage_name": "Review Quilgo Assessments",
    },
    "non-tech": {
        "job_id":            3635455,
        "label":             "Non-Tech Pipeline",
        "source_stage_id":   1896269,
        "source_stage_name": "(NEW) Review Tests",
    },
}

# Slug → human-readable test name
SLUG_TO_TEST_NAME = {
    "apis":            "APIs & Postman",
    "javascript":      "JavaScript",
    "typescrpt":       "Typescript",
    "java":            "Java",
    "pythongeneral":   "Python: General",
    "selenium":        "Selenium",
    "cypress":         "Cypress",
    "excel":           "Excel",
    "sql":             "SQL",
    "pythondata":      "Python: Data",
    "dataviztableau":  "Data Viz: Tableau",
    "datavizpowerbi":  "Data Viz: PowerBI",
    "datavizlooker":   "Data Viz: Looker",
    "statistics":      "Statistics",
    "machinelearning": "Machine Learning",
    "networking":      "Networking",
    "logserrors":      "Logs & Errors",
    "oslinux":         "OS Commands: Linux",
    "oswindows":       "OS Commands: Windows",
    "figma":           "Figma",
    "adobexd":         "Adobe XD",
    "sketch":          "Sketch",
    "git":             "Git & CI/CD",
    "aws":             "AWS",
    "azure":           "Azure",
    "docker":          "Docker",
    "kubernetes":      "Kubernetes",
}

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def load_api_key() -> str:
    if not CONFIG_FILE.exists():
        sys.exit(f"❌  Config file not found: {CONFIG_FILE}")
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    key = cfg.get("credentials", "manatal_api_key", fallback="").strip()
    if not key:
        sys.exit("❌  'manatal_api_key' not found in gui_config.ini [credentials].")
    return key


def api_get(url: str, headers: dict) -> dict | list | None:
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        print(f"    ⚠️  GET failed — {url}\n       {e}")
        return None


def strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;",   "<", text)
    text = re.sub(r"&gt;",   ">", text)
    text = re.sub(r"&amp;",  "&", text)
    text = re.sub(r" {2,}",  " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Step 1 — resolve candidate ID from email
# ---------------------------------------------------------------------------
def find_candidate_id(email: str, headers: dict) -> int | None:
    """
    Tries several search strategies to resolve a Manatal candidate ID from
    an email address without scanning all candidates.

    Strategies (in order):
      a) ?search=<full email>
      b) ?search=<username>  (part before @, e.g. "amosndungo")
      c) ?email=<full email>  (direct field filter — some API versions support this)
    """
    username = email.split("@")[0]
    encoded_email = quote(email, safe="@")
    strategies = [
        f"{BASE_URL}/candidates/?search={encoded_email}&page_size=20",
        f"{BASE_URL}/candidates/?search={username}&page_size=20",
        f"{BASE_URL}/candidates/?email={encoded_email}&page_size=20",
    ]

    for url in strategies:
        data = api_get(url, headers)
        if not data:
            continue
        results = data if isinstance(data, list) else data.get("results", [])
        for c in results:
            if str(c.get("email", "")).lower().strip() == email.lower():
                return c.get("id")
        time.sleep(0.2)

    return None


# ---------------------------------------------------------------------------
# Step 2 — fetch that candidate's match in a specific pipeline
# ---------------------------------------------------------------------------
def find_match_for_candidate(candidate_id: int, job_id: int, headers: dict) -> dict | None:
    """
    Scans all matches for a job until it finds one belonging to candidate_id.
    The Manatal API ignores the ?candidate= filter and returns paginated results,
    so we always do a full scan rather than trusting an empty first page as "not found".
    """
    url = f"{BASE_URL}/jobs/{job_id}/matches/?page_size=100"
    page = 0
    while url and page < 20:
        data = api_get(url, headers)
        if not data:
            break
        for m in (data.get("results") or []):
            if m.get("candidate") == candidate_id:
                return m
        url = data.get("next")
        page += 1
        time.sleep(0.15)

    return None


# ---------------------------------------------------------------------------
# Core — build one candidate's full report
# ---------------------------------------------------------------------------
def build_candidate_report(email: str, headers: dict) -> dict:
    report = {
        "email":          email,
        "candidate_id":   None,
        "basic_info":     {},
        "custom_fields":  {},
        "pipelines":      [],
        "fetch_time_utc": datetime.now(timezone.utc).isoformat(),
    }

    # Step 1: resolve candidate ID
    print(f"    Searching for candidate ID…")
    candidate_id = find_candidate_id(email, headers)
    if not candidate_id:
        report["error"] = "Could not resolve candidate ID — not found via any search strategy."
        return report

    print(f"    ✔ Candidate ID: {candidate_id}")
    report["candidate_id"] = candidate_id

    # Step 2: fetch full profile directly
    profile = api_get(f"{BASE_URL}/candidates/{candidate_id}/", headers)
    if not profile:
        report["error"] = f"Could not fetch profile for candidate ID {candidate_id}."
        return report

    report["basic_info"] = {
        "full_name":    f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip(),
        "email":        profile.get("email"),
        "phone":        profile.get("phone") or "—",
        "location":     profile.get("location") or "—",
        "headline":     profile.get("headline") or "—",
        "candidate_id": candidate_id,
        "manatal_url":  f"https://app.manatal.com/candidate/{candidate_id}",
    }
    report["custom_fields"] = profile.get("custom_fields") or {}

    # Step 3: find this candidate's match in each pipeline
    for category, cfg in JOB_CONFIGS.items():
        job_id = cfg["job_id"]
        print(f"    Checking {cfg['label']} (job {job_id})…")

        match = find_match_for_candidate(candidate_id, job_id, headers)
        if not match:
            print(f"      Not found in this pipeline.")
            continue

        match_pk  = match.get("id")
        stage_obj = match.get("stage") or {}
        current_stage_id = stage_obj.get("id")

        pipeline_entry = {
            "pipeline_category":  category,
            "pipeline_label":     cfg["label"],
            "job_id":             job_id,
            "source_stage_id":    cfg["source_stage_id"],
            "source_stage_name":  cfg["source_stage_name"],
            "match_pk":           match_pk,
            "is_active":          match.get("is_active"),
            "current_stage_id":   current_stage_id,
            "current_stage_name": stage_obj.get("name", "Unknown"),
            "notes":              [],
        }

        # Step 4: fetch notes for this match
        notes_data = api_get(f"{BASE_URL}/matches/{match_pk}/notes/", headers)
        if notes_data:
            notes_list = notes_data if isinstance(notes_data, list) else notes_data.get("results", [])
            for note in notes_list:
                pipeline_entry["notes"].append({
                    "note_id":      note.get("id"),
                    "created":      note.get("created", "—"),
                    "content_html": note.get("info", ""),
                    "content_text": strip_html(note.get("info", "")),
                })

        moved = current_stage_id != cfg["source_stage_id"]
        print(f"      Stage: [{current_stage_id}] {stage_obj.get('name', 'Unknown')}"
              + ("  ✔ MOVED" if moved else "  (source stage)"))
        report["pipelines"].append(pipeline_entry)

    return report


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
SEP2 = "═" * 72

def print_kv(key: str, value, indent: int = 4):
    print(f"{'':>{indent}}{key:<34} {value}")


def print_report(report: dict):
    print(f"\n{SEP2}")
    print(f"  CANDIDATE: {report['email']}")
    print(SEP2)

    if report.get("error"):
        print(f"  ❌  {report['error']}")
        return

    bi = report["basic_info"]
    print("\n  ┌─ BASIC INFO")
    print_kv("Full name",    bi.get("full_name", "—"))
    print_kv("Email",        bi.get("email", "—"))
    print_kv("Phone",        bi.get("phone", "—"))
    print_kv("Location",     bi.get("location", "—"))
    print_kv("Headline",     bi.get("headline", "—"))
    print_kv("Candidate ID", bi.get("candidate_id", "—"))
    print_kv("Manatal URL",  bi.get("manatal_url", "—"))

    cf = report["custom_fields"]
    print("\n  ┌─ CUSTOM FIELDS  (quiz scores + outcome)")
    if not cf:
        print("    (none — scores have not been pushed yet)")
    else:
        passed_val = cf.get("techtestspassed")
        print_kv("techtestspassed  →  OUTCOME", passed_val if passed_val is not None else "(not set yet)")

        print()
        score_fields_found = False
        for slug, human_name in SLUG_TO_TEST_NAME.items():
            val = cf.get(slug)
            if val is not None:
                score_fields_found = True
                print_kv(f"  {slug}  ({human_name})", val)
        if not score_fields_found:
            print("    No individual quiz score fields set yet.")

        known = set(SLUG_TO_TEST_NAME) | {"techtestspassed"}
        extras = {k: v for k, v in cf.items() if k not in known and v is not None}
        if extras:
            print("\n    Other custom fields:")
            for k, v in extras.items():
                print_kv(f"  {k}", v)

    print("\n  ┌─ PIPELINE POSITIONS")
    if not report["pipelines"]:
        print("    Not found in any job pipeline.")
    else:
        for pe in report["pipelines"]:
            moved = pe["current_stage_id"] != pe["source_stage_id"]
            active_str = "ACTIVE" if pe.get("is_active") else "inactive"

            print(f"\n    Pipeline       : {pe['pipeline_label']}  [{pe['pipeline_category']}]")
            print(f"    Job ID         : {pe['job_id']}")
            print(f"    Match PK       : {pe['match_pk']}   (status: {active_str})")
            print(f"    Pulled from    : [{pe['source_stage_id']}]  {pe['source_stage_name']}")
            print(f"    Current stage  : [{pe['current_stage_id']}]  {pe['current_stage_name']}"
                  + ("  ✔ MOVED" if moved else "  (still in source stage)"))

            notes = pe.get("notes", [])
            if not notes:
                print("    Notes          : (none)")
            else:
                print(f"    Notes          : {len(notes)} note(s)")
                for i, note in enumerate(notes, 1):
                    text    = note.get("content_text", "")
                    preview = text[:700] + ("…" if len(text) > 700 else "")
                    print(f"\n      ── Note {i}  (created: {note.get('created', '—')}) ──")
                    for line in preview.splitlines():
                        print(f"         {line}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")

    print(SEP2)
    print("  Manatal Test-Candidate Monitor")
    print(f"  Run at  : {timestamp}")
    print(f"  Watching: {', '.join(sorted(TEST_EMAILS))}")
    print(SEP2)

    api_key = load_api_key()
    headers = {"accept": "application/json", "Authorization": f"Token {api_key}"}
    print(f"\n✔ API key loaded  (last 6 chars: …{api_key[-6:]})")

    all_reports = []
    for email in sorted(TEST_EMAILS):
        print(f"\n{'─'*72}")
        print(f"  Fetching: {email}")
        print(f"{'─'*72}")
        report = build_candidate_report(email, headers)
        print_report(report)
        all_reports.append(report)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{timestamp}_test_candidates.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_reports, f, indent=2, default=str)

    print(f"\n{SEP2}")
    print(f"  💾  Saved → {out_path.relative_to(PROJECT_ROOT)}")
    print(f"  ✅  Done.")
    print(SEP2)


if __name__ == "__main__":
    main()
