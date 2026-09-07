import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './browser-tests',
  timeout: 45000,
  fullyParallel: true,
  workers: 2,
  reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:8765/Job_Scraper/', browserName: 'chromium', trace: 'retain-on-failure' },
  webServer: {
    command: 'python3 -m http.server 8765 --bind 127.0.0.1 --directory ..',
    url: 'http://127.0.0.1:8765/Job_Scraper/triage.html',
    reuseExistingServer: !process.env.CI,
  },
});
