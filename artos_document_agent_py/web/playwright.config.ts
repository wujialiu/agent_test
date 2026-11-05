import { fileURLToPath } from "node:url";
import { defineConfig } from "@playwright/test";

const runDirectory = fileURLToPath(new URL("../../.agent_lab/browser/", import.meta.url));

export default defineConfig({
  testDir: "./tests",
  outputDir: `${runDirectory}results`,
  reporter: [
    ["list"],
    ["html", { outputFolder: `${runDirectory}report`, open: "never", title: "Study Desk · Chrome workflow tests" }],
  ],
  fullyParallel: true,
  workers: 2,
  retries: 0,
  timeout: 45_000,
  projects: [{
    name: "Google Chrome",
    use: {
      channel: "chrome",
      viewport: { width: 1440, height: 1000 },
      screenshot: "only-on-failure",
      trace: "retain-on-failure",
    },
  }],
});
