# Quilgo Automation Suite

An end-to-end recruitment automation tool that downloads candidate assessment reports from Quilgo, evaluates them against Manatal ATS profiles, and pushes structured results back to Manatal — all through a browser-based dashboard.

---

## How It Works

The suite runs in two sequential parts:

```
Part 1 — Downloader          Part 2 — Processor             Part 3 — Push
─────────────────────        ──────────────────────         ──────────────────
Playwright logs into    →    Python fetches Manatal    →    Scores & decisions
Quilgo, exports each         profiles, cross-references      are written back
quiz CSV report, and         Quilgo data, evaluates          to each candidate
saves to Quilgo/master/      each candidate by role          in Manatal via API
                             category, and surfaces
                             a Final Review dashboard
```

**Role categories** drive evaluation logic:

| Category    | Qualification rule |
|-------------|-------------------|
| `tech`      | ≥ 2 test scores ≥ 7/10 **and** no critical integrity flags |
| `non-tech`  | No score threshold — suspicious activity signals only |

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Docker Desktop** | [docker.com/get-started](https://www.docker.com/get-started) — the only thing you need to install |
| **Quilgo account** | Email + password with access to quiz results |
| **Manatal API key** | Found in your Manatal workspace settings |

---

## Quick Start — Docker (Recommended)

### 1. Clone the repository

```bash
git clone https://github.com/Tana-Training-Team/Quilgo-Automation-Suite-GUI.git
cd Quilgo-Automation-Suite-GUI
```

### 2. Start the container

```bash
docker-compose up --build -d
```

The first build takes 3–5 minutes — it installs Python packages, Node.js, and downloads the Playwright Chromium browser inside the container. Subsequent starts are instant.

### 3. Open the dashboard

```
http://localhost:8501
```

### 4. Enter your credentials (one time only)

Navigate to **Settings** in the sidebar and fill in:

- Quilgo email and password
- Manatal API key

Credentials are saved to `gui_config.ini` on your host machine (mounted as a volume) and persist across container restarts.

### 5. Run the automation

Go to the **Control Panel** and follow the three-step flow:

```
[ Run Part 1 ]  →  [ Run Part 2 ]  →  [ Push to Manatal ]
  Downloads          Processes          Writes scores back
  Quilgo CSVs        & evaluates        to each candidate
```

**Optional controls during Part 1:**
- **Stop** — cancels the download entirely
- **Stop & Continue to Part 2 →** — stops downloading further quizzes but immediately hands off whatever was already downloaded to Part 2

---

## Stopping and Restarting

```bash
# Stop the container (data is preserved in volumes)
docker-compose down

# Start again without rebuilding
docker-compose up -d

# Rebuild after pulling code changes
docker-compose up --build -d

# View live logs
docker-compose logs -f
```

---

## Data Persistence

All outputs survive container restarts through Docker volume mounts:

| Host path | Container path | Contents |
|-----------|---------------|----------|
| `./Quilgo/` | `/app/Quilgo/` | Downloaded quiz CSVs |
| `./downloads/` | `/app/downloads/` | Manatal cache, audit logs |
| `./gui_config.ini` | `/app/gui_config.ini` | Saved credentials |
| `./test-results/` | `/app/test-results/` | Playwright failure traces |
| `./playwright-report/` | `/app/playwright-report/` | Playwright HTML report |

### Storage layout

```
Quilgo/
├── master/              ← active CSV files (current run)
│   ├── manifest.json    ← per-file creation time, row count, new rows this run
│   ├── SQL.csv
│   ├── JavaScript.csv
│   └── ...
└── backup/              ← previous run (rotated automatically before each Part 1)
    ├── manifest.json
    └── ...
```

Before every Part 1 run, `master/` is rotated to `backup/` and a fresh `master/` is created. At most two run-equivalents of data are kept at any time.

---

## Filtering Options

Both the quiz selection and the date range can be narrowed before running:

- **Quiz filter** — select one or more quizzes in the Control Panel; unselected quizzes are skipped entirely during download
- **Date filter** — restrict Part 2 processing to submissions within a specific date window

---

## Integrity Signals

The processor evaluates four integrity signals per candidate per test:

| Signal | Threshold | Action |
|--------|-----------|--------|
| Trust Score | Not "High" | Flag for manual review |
| Face Presence | < 15 % | Flag for manual review |
| Tab Switches | 1–4 | Flag for manual review |
| Tab Switches | ≥ 5 | Automatic disqualification |
| Camera Tracking | Disabled | Flag for manual review |

Specific flags are surfaced on each candidate's review card, e.g.:
> *JavaScript: Low Face Presence (8%), Suspicious Tab Switches (3)*

---

## Final Review Dashboard

After Part 2 completes, the **Final Review** page shows every candidate with:

- Overall status: `QUALIFIED` / `BORDERLINE` / `MANUAL REVIEW` / `DISQUALIFIED`
- Test scores inline: `APIs & Postman: 8/10  |  JavaScript: 7/10`
- Integrity flags per test
- Controls to override the automated decision before pushing

---

## Local Setup (Alternative)

> Use this only if you cannot run Docker. Docker is strongly preferred because it handles all browser dependencies automatically.

**Requirements:** Python 3.12, Node.js 20

```bash
# Python dependencies
pip install -r requirements.txt

# Node.js dependencies
npm install

# Playwright browser (downloads ~150 MB)
npx playwright install chromium --with-deps

# Start the app
streamlit run streamlit_app.py
```

Open `http://localhost:8501` in your browser.

---

## Project Structure

```
.
├── streamlit_app.py          # Entry point — Streamlit web UI
├── app/
│   ├── config.py             # Paths and theme constants
│   ├── automation/
│   │   └── task_manager.py   # Threading, subprocess management
│   └── ui/
│       ├── control_panel_view.py
│       └── final_review_view.py
├── core/
│   ├── processor.py          # Main orchestrator
│   └── processing/
│       ├── manatal_fetcher.py
│       ├── quilgo_parser.py   # MASTER_TEST_CONFIG, role categories
│       ├── candidate_evaluator.py
│       ├── file_helpers.py    # master/backup rotation, manifest
│       └── api_pusher.py
├── tests/
│   └── downloader.spec.ts    # Playwright downloader
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── package.json
```

---

## Troubleshooting

**Container won't start**
```bash
docker-compose logs quilgo-suite
```
Most startup failures are port conflicts (`8501` already in use) or missing `gui_config.ini`.

**Part 1 hangs on a specific quiz**
The downloader retries each quiz up to 3 times with a 45-second timeout per attempt. If all retries fail, an empty placeholder CSV is written so Part 2 can continue. Check `./test-results/` for Playwright traces.

**Part 2 finds 0 candidates**
Verify the Manatal API key is valid and that the target pipeline stage contains active candidates. The processor matches Quilgo emails against Manatal profiles — candidates must exist in both systems.

**Credentials not saved between runs**
Ensure `gui_config.ini` exists on the host before starting the container. If it doesn't exist yet, start the container once, enter credentials in Settings, save — Docker will create the file.
