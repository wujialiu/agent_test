import { readFile } from "node:fs/promises";
import type { Workspace } from "../src/api";
import {
  approve, approveBaseline, browserRequest, button, capture, draft, expect, generateRequest, scenario,
  section, sectionButton, selectedSnapshot, snapshot, test, workspace,
} from "./workspace";

const missing = "study-b202 · B202 missing Week 12 safety";
const corrected = "study-b202 · B202 corrected Week 12 safety";

test.describe("Happy paths", { tag: "@happy" }, () => {
  test("Generate an efficacy section and trace the claim to its exact row", async ({ app, page }, info) => {
    await page.goto(app.url);
    await test.step("Load the fictional baseline and generate Week 12 efficacy", async () => {
      await scenario(page, "Load baseline", "Baseline");
      await section(page, "Week 12 efficacy");
      await draft(page);
      await expect(page.locator(".claims")).toContainText("31/50 participants (62.0%)");
      await expect(page.locator(".claims")).toContainText("20/50 participants (40.0%)");
    });
    await test.step("Open the active-arm citation and check source, population, arm and time point", async () => {
      await button(page, "View source").first().click();
      const evidence = page.locator(".evidence details[open]");
      await expect(evidence).toContainText("efficacy.csv");
      await expect(evidence).toContainText("CSV line 2");
      await expect(evidence).toContainText("itt");
      await expect(evidence).toContainText("active");
      await expect(evidence).toContainText("week-12");
      await expect(evidence).toContainText("SHA-256");
      await capture(page, info, "Efficacy claim and its source row");
    });
  });

  test("Approve all four sections and download the reviewable JSON", async ({ app, page }, info) => {
    await page.goto(app.url);
    await test.step("Generate and approve all four sections", async () => {
      await approveBaseline(page);
      await expect(button(page, "Export approved summary")).toBeEnabled();
    });
    await test.step("Download the summary and verify all four approvals and claim references", async () => {
      const downloading = page.waitForEvent("download");
      await button(page, "Export approved summary").click();
      const download = await downloading;
      expect(download.suggestedFilename()).toBe("study-a101-approved-summary.json");
      const document = JSON.parse(await readFile(await download.path(), "utf8")) as Workspace;
      expect(document.sections).toHaveLength(4);
      for (const row of document.sections) {
        expect(row.revision?.review?.decision).toBe("approve");
        for (const claim of row.revision?.draft.claims ?? []) {
          expect(row.revision?.draft.evidence.some((entry) => entry.id === claim.evidence_id)).toBe(true);
        }
      }
      await expect(page.getByText("Approved summary exported.")).toBeVisible();
      await capture(page, info, "Four approved sections and export");
    });
  });

  test("Regenerate only the affected section after a safety amendment", async ({ app, page }, info) => {
    await page.goto(app.url);
    await test.step("Approve the baseline, then switch to the amended source snapshot", async () => {
      await approveBaseline(page);
      await scenario(page, "Load amendment", "Amended safety");
      await expect(sectionButton(page, "Week 12 safety")).toContainText("Source changed");
      for (const title of ["Study design", "Enrollment", "Week 12 efficacy"]) {
        await expect(sectionButton(page, title)).toContainText("Approved");
      }
      await expect(button(page, "Export approved summary")).toBeDisabled();
    });
    await test.step("Check the revised count and the original source in history", async () => {
      await draft(page, 2);
      await expect(page.locator(".claims")).toContainText("6/49 participants (12.2%)");
      await page.locator(".history > summary").click();
      const older = page.locator(".history-item").filter({ hasText: "Revision 1" });
      await older.locator("summary").first().click();
      await expect(older).toContainText("5/49 participants (10.2%)");
      await expect(older.getByText("safety-v1.csv", { exact: true }).first()).toBeVisible();
      await approve(page);
      await expect(button(page, "Export approved summary")).toBeEnabled();
      await capture(page, info, "New safety revision and preserved prior evidence");
    });
  });

  test("Request changes, regenerate, and keep the earlier review decision", async ({ app, page }, info) => {
    await page.goto(app.url);
    await scenario(page, "Load baseline", "Baseline");
    await draft(page);
    await test.step("Record changes requested with a reviewer note", async () => {
      await page.getByRole("textbox", { name: "Reviewer name" }).fill("Test reviewer");
      await page.getByRole("textbox", { name: "Review note" }).fill("Recheck the source wording.");
      await button(page, "Request changes").click();
      await expect(sectionButton(page, "Study design")).toContainText("Changes requested");
    });
    await test.step("Create a fresh revision and inspect the earlier note", async () => {
      await draft(page, 2);
      await expect(sectionButton(page, "Study design")).toContainText("Ready for review");
      await page.locator(".history > summary").click();
      const older = page.locator(".history-item").filter({ hasText: "Revision 1" });
      await older.locator("summary").first().click();
      await expect(older).toContainText("Recheck the source wording.");
      await capture(page, info, "Review note retained in revision history");
    });
  });

  test("Import the B202 correction and unlock export after review", async ({ app, page }, info) => {
    await app.importB202("missing");
    await page.goto(app.url);
    await expect(page.getByLabel("Source snapshot")).toBeVisible();
    await test.step("Approve the three unaffected sections in B202 and draft safety", async () => {
      for (const title of ["Study design", "Enrollment", "Week 12 efficacy"]) {
        await section(page, title);
        await draft(page);
        await approve(page);
      }
      await section(page, "Week 12 safety");
      await draft(page);
      await expect(sectionButton(page, "Week 12 safety")).toContainText("QC blocked");
    });
    await test.step("Import the correction, confirm the new source location, and reapprove safety", async () => {
      await app.importB202("corrected");
      await page.reload();
      await snapshot(page, corrected);
      await section(page, "Week 12 safety");
      await expect(sectionButton(page, "Week 12 safety")).toContainText("Source changed");
      await draft(page, 2);
      await expect(page.locator(".claims")).toContainText("5/69 participants (7.2%)");
      await button(page, "View source").nth(1).click();
      const evidence = page.locator(".evidence details[open]");
      await expect(evidence).toContainText("safety-v2.json");
      await expect(evidence).toContainText("/facts/3");
      await approve(page);
      await expect(button(page, "Export approved summary")).toBeEnabled();
      await capture(page, info, "B202 correction with exact source location");
    });
  });

  test("Keep approval and audit history after reload at mobile width", async ({ app, page }, info) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(app.url);
    await scenario(page, "Load baseline", "Baseline");
    await draft(page);
    await approve(page);
    await test.step("Reload the page and check the stored approval and audit events", async () => {
      await page.reload();
      await expect(sectionButton(page, "Study design")).toContainText("Approved");
      await page.locator(".audit > summary").click();
      for (const event of ["snapshot ingested", "draft created", "review recorded"]) {
        await expect(page.locator(".audit").getByText(event, { exact: true })).toBeVisible();
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
      await capture(page, info, "Mobile review and audit log");
    });
  });
});

test.describe("Expected blocks", { tag: "@blocked" }, () => {
  test("Block safety approval when two sources disagree on the same result", async ({ app, page }, info) => {
    await page.goto(app.url);
    await scenario(page, "Load conflict", "Conflicting safety sources");
    await draft(page);
    await test.step("Display both conflicting rows and block approval in the interface", async () => {
      await expect(page.getByText("active · conflicting evidence")).toBeVisible();
      await button(page, "Source 1").click();
      await button(page, "Source 2").click();
      await expect(page.locator(".evidence details[open]")).toHaveCount(2);
      await page.getByRole("textbox", { name: "Reviewer name" }).fill("Test reviewer");
      await expect(button(page, "Approve")).toBeDisabled();
      await capture(page, info, "Conflicting evidence and disabled approval");
    });
    await test.step("Reject a direct approval request from Chrome with HTTP 409", async () => {
      const id = await selectedSnapshot(page);
      const result = await browserRequest(page, `/snapshots/${id}/sections/safety/review`, {
        revision: 1, decision: "approve", reviewer: "Test reviewer",
      });
      expect(result.status).toBe(409);
      expect(result.data.detail).toContain("Resolve the QC findings");
    });
  });

  test("Block a missing result even when similar population and time-point rows exist", async ({ app, page }, info) => {
    await app.importB202("missing");
    await page.goto(app.url);
    await snapshot(page, missing);
    await section(page, "Week 12 safety");
    await draft(page);
    await test.step("Keep the active claim and flag the exact missing placebo scope", async () => {
      await expect(page.locator(".claims")).toContainText("9/71 participants (12.7%)");
      await expect(page.locator(".claims")).not.toContainText("Placebo arm");
      await expect(page.getByText("placebo · missing evidence")).toBeVisible();
      await expect(page.locator(".findings")).toContainText(
        "No row matches participants_with_ae · safety · placebo · week-12.",
      );
      await page.getByRole("textbox", { name: "Reviewer name" }).fill("Test reviewer");
      await expect(button(page, "Approve")).toBeDisabled();
      await expect(button(page, "Export approved summary")).toBeDisabled();
      await capture(page, info, "Missing placebo evidence");
    });
  });

  test("Block export before review and require a note for requested changes", async ({ app, page }, info) => {
    await page.goto(app.url);
    await scenario(page, "Load baseline", "Baseline");
    await draft(page);
    const id = await selectedSnapshot(page);
    await test.step("Reject export when current approvals are missing", async () => {
      await expect(button(page, "Export approved summary")).toBeDisabled();
      const exported = await browserRequest(page, `/snapshots/${id}/export`);
      expect(exported.status).toBe(409);
      expect(exported.data.detail).toContain("requires current approvals");
    });
    await test.step("Enforce the review-note requirement in the UI and the API", async () => {
      await page.getByRole("textbox", { name: "Reviewer name" }).fill("Test reviewer");
      await expect(button(page, "Request changes")).toBeDisabled();
      const review = await browserRequest(page, `/snapshots/${id}/sections/design/review`, {
        revision: 1, decision: "request_changes", reviewer: "Test reviewer",
      });
      expect(review.status).toBe(422);
      await capture(page, info, "Unreviewed revision and required note");
    });
  });

  test("Reject a stale approval and an older revision number", async ({ app, page }, info) => {
    await page.goto(app.url);
    await scenario(page, "Load baseline", "Baseline");
    await section(page, "Week 12 safety");
    await draft(page);
    await scenario(page, "Load amendment", "Amended safety");
    const id = await selectedSnapshot(page);
    const path = `/snapshots/${id}/sections/safety/review`;
    const decision = { revision: 1, decision: "approve", reviewer: "Test reviewer" };
    await test.step("Block an approval tied to the outdated source snapshot", async () => {
      await expect(sectionButton(page, "Week 12 safety")).toContainText("Source changed");
      const stale = await browserRequest(page, path, decision);
      expect(stale.status).toBe(409);
      expect(stale.data.detail).toContain("Evidence changed");
      await capture(page, info, "Stale safety draft");
    });
    await test.step("Regenerate, then reject an approval for the superseded revision", async () => {
      await draft(page, 2);
      const superseded = await browserRequest(page, path, decision);
      expect(superseded.status).toBe(409);
      expect(superseded.data.detail).toContain("revision 2");
    });
  });

  test("Save one revision when a draft request is retried or races another request", async ({ app, page }) => {
    await page.goto(app.url);
    await scenario(page, "Load baseline", "Baseline");
    const id = await selectedSnapshot(page);
    const base = `/snapshots/${id}/sections`;
    await test.step("Replay an identical request and reject reuse of its ID for a different section", async () => {
      const first = await generateRequest(page, `${base}/design`, "same-id");
      const replay = await generateRequest(page, `${base}/design`, "same-id");
      expect(first.status).toBe(200);
      expect(replay.data).toEqual(first.data);
      const collision = await generateRequest(page, `${base}/disposition`, "same-id");
      expect(collision.status).toBe(409);
    });
    await test.step("Race two draft requests from revision zero and verify one winner", async () => {
      const results = await Promise.all([
        generateRequest(page, `${base}/safety`, "writer-a"),
        generateRequest(page, `${base}/safety`, "writer-b"),
      ]);
      expect(results.map((result) => result.status).sort()).toEqual([200, 409]);
      const current = await workspace(page, id);
      expect(current.sections.find((entry) => entry.id === "safety")?.revision?.number).toBe(1);
    });
  });

  test("Reject a cross-origin write sent by a browser page", async ({ app, page }) => {
    await page.goto(app.url.replace("127.0.0.1", "localhost"));
    const path = "/api/scenarios/baseline";
    const denied = page.waitForResponse((response) => response.url() === app.url + path);
    await page.evaluate(async (target) => {
      await fetch(target, { method: "POST", mode: "no-cors" });
    }, app.url + path);
    expect((await denied).status()).toBe(403);
    const response = await browserRequest<unknown[]>(page, "/snapshots");
    expect(response.data).toEqual([]);
  });
});
