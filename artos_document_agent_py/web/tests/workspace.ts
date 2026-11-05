/** Start the real app with a fresh SQLite database for every Chrome case. */
import { execFile, spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { expect, test as base, type Page, type TestInfo } from "@playwright/test";
import type { Revision, Workspace } from "../src/api";

const run = promisify(execFile);
const repo = fileURLToPath(new URL("../../../", import.meta.url));
const python = join(repo, ".venv/bin/python");

export const test = base.extend<{ app: { url: string; importB202: (version: "missing" | "corrected") => Promise<void> } }>({
  app: async ({}, use) => {
    const directory = await mkdtemp(join(tmpdir(), "study-desk-chrome-"));
    const db = join(directory, "workspace.sqlite3");
    const args = ["-m", "artos_document_agent_py", "--db", db];
    const server = spawn(python, [...args, "serve", "--port", "0"], { cwd: repo });
    const stopped = new Promise<void>((resolve) => server.once("close", () => resolve()));

    try {
      const url = await new Promise<string>((resolve, reject) => {
        let log = "";
        const timeout = setTimeout(() => reject(new Error(`The test app did not start.\n${log}`)), 10_000);
        server.stderr.on("data", (chunk) => {
          log += chunk.toString();
          const match = log.match(/Uvicorn running on (http:\/\/127\.0\.0\.1:\d+)/);
          if (match) {
            clearTimeout(timeout);
            resolve(match[1]);
          }
        });
        server.once("error", (error) => {
          clearTimeout(timeout);
          reject(error);
        });
        server.once("exit", () => {
          clearTimeout(timeout);
          reject(new Error(`The test app stopped.\n${log}`));
        });
      });
      await use({
        url,
        async importB202(version) {
          // B202 uses the real operator import command. The review steps use Chrome.
          await run(python, [...args, "ingest", `artos_document_agent_py/practice_data/b202-${version}.json`], { cwd: repo });
        },
      });
    } finally {
      server.kill("SIGTERM");
      await stopped;
      await rm(directory, { recursive: true, force: true });
    }
  },
});

export function button(page: Page, name: string) {
  return page.getByRole("button", { name, exact: true });
}

export function sectionButton(page: Page, title: string) {
  return page.getByRole("navigation", { name: "Document sections" })
    .getByRole("button", { name: new RegExp(`^${title}`) });
}

export async function section(page: Page, title: string) {
  await sectionButton(page, title).click();
  await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();
}

export async function scenario(page: Page, label: string, snapshotLabel: string) {
  await button(page, label).click();
  await expect(page.getByText(`${snapshotLabel} selected.`, { exact: true })).toBeVisible();
}

export async function snapshot(page: Page, label: string) {
  const picker = page.getByLabel("Source snapshot");
  await picker.selectOption({ label });
  await expect(picker).toBeEnabled();
  await expect(picker.locator("option:checked")).toHaveText(label);
}

export async function draft(page: Page, revision = 1) {
  await button(page, revision === 1 ? "Generate draft" : "Regenerate section").click();
  await expect(page.getByText(`Revision ${revision} saved.`, { exact: true })).toBeVisible();
}

export async function approve(page: Page) {
  await page.getByRole("textbox", { name: "Reviewer name" }).fill("Test reviewer");
  await button(page, "Approve").click();
  await expect(page.getByText("Revision approved.", { exact: true })).toBeVisible();
}

export async function approveBaseline(page: Page) {
  await scenario(page, "Load baseline", "Baseline");
  for (const title of ["Study design", "Enrollment", "Week 12 efficacy", "Week 12 safety"]) {
    await section(page, title);
    await draft(page);
    await approve(page);
  }
}

export async function capture(page: Page, info: TestInfo, title: string) {
  await info.attach(title, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
}

export async function selectedSnapshot(page: Page) {
  return page.getByLabel("Source snapshot").inputValue();
}

// Call the API from the actual page origin to verify that safeguards apply
// even when a caller bypasses a disabled UI button.
export async function browserRequest<T = { detail: string }>(
  page: Page, route: string, body?: object,
): Promise<{ status: number; data: T }> {
  return page.evaluate(async ({ path, input }) => {
    const response = await fetch(`/api${path}`, input ? {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    } : undefined);
    return { status: response.status, data: await response.json() };
  }, { path: route, input: body });
}

export async function workspace(page: Page, id: string) {
  const result = await browserRequest<Workspace>(page, `/snapshots/${id}/workspace`);
  expect(result.status).toBe(200);
  return result.data;
}

export async function generateRequest(page: Page, path: string, requestId: string, expected = 0) {
  return browserRequest<Revision>(page, `${path}/draft`, {
    expected_revision: expected, request_id: requestId,
  });
}

export { expect };
