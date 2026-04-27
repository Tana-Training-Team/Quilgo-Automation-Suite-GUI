// --- MODIFICATION 1: Import dotenv/config ---
// This line MUST be at the very top. It automatically finds and loads
// the variables from the .env file that our Python script will create.
import 'dotenv/config';

import { test, expect, Page } from '@playwright/test';
import path from 'path';
import fs from 'fs';

// ==================================================================================
// --- UTILITY FUNCTIONS ---
// ==================================================================================

function getMasterDownloadFolder(): string {
  // Python rotates master → backup before spawning this script,
  // so by the time we get here master/ is a fresh empty directory.
  const masterDir = path.join(__dirname, '..', 'Quilgo', 'master');
  fs.mkdirSync(masterDir, { recursive: true });
  console.log(`Using master download directory: ${masterDir}`);
  return masterDir;
}

function sanitizeFilename(name: string): string {
  return name.replace(/[:\\/?*|"<>]/g, '_');
}

// ==================================================================================
// --- CORE ACTION FUNCTIONS ---
// ==================================================================================

async function performLogin(page: Page, email: string, password: string) {
  console.log('--- Performing full login ---');
  await page.goto('https://quilgo.com/login', { waitUntil: 'domcontentloaded' });
  await page.getByRole('textbox', { name: 'you@example.com' }).fill(email);
  await page.getByRole('textbox', { name: 'Password' }).fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  console.log('Login successful.');
}

async function downloadReportForQuiz(page: Page, quizName: string, downloadDirectory: string) {
    console.log(`--- Starting download action for: "${quizName}" ---`);
    // Bound the wait so a single stuck quiz cannot drain the whole test budget.
    // 45 s is generous for a CSV export; anything longer is almost certainly a hang.
    const downloadPromise = page.waitForEvent('download', { timeout: 45000 });
    await page.getByText('Export', { exact: true }).click({ timeout: 25000 });
    const download = await downloadPromise;
    console.log(`✅ Download event received! Suggested filename: ${download.suggestedFilename()}`);
    
    const sanitizedQuizName = sanitizeFilename(quizName);
    const fileExtension = path.extname(download.suggestedFilename());
    const newFilename = `${sanitizedQuizName}${fileExtension}`;
    const downloadPath = path.join(downloadDirectory, newFilename);

    await download.saveAs(downloadPath);
    console.log(`✅ Download successful! File for "${quizName}" saved to: ${downloadPath}`);
}

// ==================================================================================
// --- MAIN TEST ORCHESTRATOR ---
// ==================================================================================
test('Login and download all quiz reports with retries and resume', async ({ page }) => {
  // 30 min overall budget. With ~27 quizzes × (≤45 s download + UI + retries),
  // 10 min was borderline and one stuck quiz (e.g. "Power BI" never firing the
  // download event) could consume the entire budget on a single waitForEvent.
  // The per-download wait is now bounded to 45 s, so this ceiling is only hit
  // in genuinely pathological cases.
  test.setTimeout(30 * 60 * 1000);

  const email = process.env.QUILGO_EMAIL;
  const password = process.env.QUILGO_PASSWORD;
  if (!email || !password) {
    throw new Error('Missing QUILGO_EMAIL or QUILGO_PASSWORD. Ensure the .env file was created by the GUI.');
  }

  const dashboardUrl = 'https://quilgo.com/app/quizzes';

  // Full list used as a fallback when no selection file exists
  const ALL_QUIZ_NAMES = [
    'SQL', 'JavaScript', 'Java', 'Python: General', 'Looker & LookML', 'Kubernetes',
    'AWS', 'APIs & Postman', 'Excel', 'Machine Learning', 'Microsoft Azure',
    'Docker', 'Networking', 'OS Commands: Linux', 'OS Commands: Windows',
    'Figma', 'Sketch', 'Error Logs', 'Cypress', 'Python: Data', 'Selenium',
    'Typescript', 'Statistics', 'Adobe XD', 'Git & CI/CD', 'Power BI', 'Tableau'
  ];

  // Read quiz selection written by the GUI (selected_quizzes.json sits at project root).
  // If the file is missing or empty we fall back to downloading everything — same as before.
  const selectionFilePath = path.join(__dirname, '..', 'selected_quizzes.json');
  let quizNames: string[];
  if (fs.existsSync(selectionFilePath)) {
    try {
      const raw = fs.readFileSync(selectionFilePath, 'utf-8');
      const parsed: string[] = JSON.parse(raw);
      quizNames = parsed.length > 0 ? parsed : ALL_QUIZ_NAMES;
      console.log(`Loaded ${quizNames.length} quiz(es) from selection file: ${quizNames.join(', ')}`);
    } catch {
      console.warn('Could not parse selected_quizzes.json — falling back to full list.');
      quizNames = ALL_QUIZ_NAMES;
    }
  } else {
    console.log('No selection file found — downloading all quizzes.');
    quizNames = ALL_QUIZ_NAMES;
  }

  const completedDownloads = new Set<string>();
  const downloadDirectory = getMasterDownloadFolder();
  await performLogin(page, email, password);

  for (const quizName of quizNames) {
    if (completedDownloads.has(quizName)) {
      console.log(`\n--- Skipping [${quizName}] (already downloaded) ---`);
      continue;
    }

    let success = false;
    const maxAttempts = 3;

    for (let attempt = 1; attempt <= maxAttempts && !success; attempt++) {
      try {
        console.log(`\n--- Attempt ${attempt}/${maxAttempts} for [${quizName}] ---`);
        await page.locator('#offcanvasSidebar').getByText(quizName, { exact: true }).click({ timeout: 60000 });
        await expect(page.getByText('Export', { exact: true })).toBeVisible({ timeout: 60000 });
        await downloadReportForQuiz(page, quizName, downloadDirectory);
        completedDownloads.add(quizName);
        success = true;
      } catch (error) {
        console.error(`❌ FAILED attempt ${attempt} for "${quizName}". Error: ${(error as Error).message}`);
        if (attempt >= maxAttempts) {
          console.error(`❌ All ${maxAttempts} attempts failed for "${quizName}". Writing empty placeholder CSV.`);
          const sanitizedName = sanitizeFilename(quizName);
          const placeholderPath = path.join(downloadDirectory, `${sanitizedName}.csv`);
          const headers = 'email,score,trust score,switched to another tab / window,face presence,camera tracking enabled,submitted (utc)\n';
          fs.writeFileSync(placeholderPath, headers, 'utf-8');
          console.log(`📄 Placeholder CSV written: ${placeholderPath}`);
        } else {
          try {
            console.log('Performing soft reset...');
            await page.goto(dashboardUrl, { waitUntil: 'domcontentloaded' });
            await expect(page.getByText('Your quizzes')).toBeVisible({ timeout: 20000 });
          } catch (resetError) {
            console.error('Soft reset failed. Performing hard reset...');
            await performLogin(page, email, password);
          }
        }
      }
    }
  }

  console.log('\n--- All quiz download tasks are complete. ---');
  console.log(`Successfully downloaded: ${completedDownloads.size} out of ${quizNames.length} files.`);
});