// playwright.config.js
const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  // Look for test files in the "tests" directory.
  testDir: './tests',
  // Timeout for each test in milliseconds.
  timeout: 60 * 1000, // 60 seconds
  // Reporter to use.
  reporter: 'list',
});