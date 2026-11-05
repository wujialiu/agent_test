"""Drive the real Study Desk in Chrome while narrating each checkpoint in Terminal."""

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import Select, WebDriverWait

ROOT = Path(__file__).resolve().parent.parent
PRACTICE = ROOT / "artos_document_agent_py" / "practice_data"
SECTIONS = ("Study design", "Enrollment", "Week 12 efficacy", "Week 12 safety")
MISSING = "study-b202 · B202 missing Week 12 safety"
CORRECTED = "study-b202 · B202 corrected Week 12 safety"
APP_COMMAND = [sys.executable, "-m", "artos_document_agent_py"]


def check(condition: bool, description: str) -> None:
    if not condition:
        raise AssertionError(description)


@dataclass(frozen=True)
class App:
    url: str
    database: Path

    def import_b202(self, revision: str) -> None:
        # This is the same local import command an operator would use.
        completed = subprocess.run(
            [
                *APP_COMMAND,
                "--db",
                str(self.database),
                "ingest",
                str(PRACTICE / f"b202-{revision}.json"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "B202 import failed.")


@contextmanager
def fresh_app() -> Iterator[App]:
    # A new port and database per case make the walkthrough repeatable.
    with TemporaryDirectory(prefix="study-desk-selenium-") as directory:
        database = Path(directory) / "workspace.sqlite3"
        logfile = Path(directory) / "server.log"
        with logfile.open("w") as log:
            process = subprocess.Popen(
                [*APP_COMMAND, "--db", str(database), "serve", "--port", "0"],
                cwd=ROOT,
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and process.poll() is None:
                    if match := re.search(
                        r"Uvicorn running on (http://127\.0\.0\.1:\d+)", logfile.read_text()
                    ):
                        yield App(match[1], database)
                        return
                    time.sleep(0.08)
                raise RuntimeError(f"Test server could not start.\n{logfile.read_text()}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


class StopWalkthrough(Exception):
    """The viewer chose to end the walkthrough at a checkpoint."""


@dataclass
class Console:
    interactive: bool

    def pause(self) -> None:
        if self.interactive:
            try:
                answer = input("       Press Enter to continue, or q to quit. ")
            except EOFError:
                raise StopWalkthrough from None
            if answer.strip().lower() == "q":
                raise StopWalkthrough

    @staticmethod
    def observed(message: str) -> None:
        print(f"  [OBSERVED] {message}")

    def show(self, observed: str, next_step: str, *, blocked: bool = False) -> None:
        prefix = "EXPECTED BLOCK" if blocked else "OBSERVED"
        print(f"  [{prefix}] {observed}")
        print(f"  [NEXT] {next_step}")
        self.pause()


class Browser:
    def __init__(self, driver: webdriver.Chrome, downloads: Path):
        self.driver = driver
        self.downloads = downloads
        self.wait = WebDriverWait(
            driver, 8, poll_frequency=0.12, ignored_exceptions=(StaleElementReferenceException,)
        )

    def visible(self, selector: str) -> WebElement:
        return self.wait.until(ec.visibility_of_element_located((By.CSS_SELECTOR, selector)))

    def text(self, selector: str, expected: str) -> None:
        self.wait.until(
            ec.text_to_be_present_in_element((By.CSS_SELECTOR, selector), expected),
            f"Expected {expected!r} in {selector}.",
        )

    def click(self, selector: str) -> None:
        self.wait.until(ec.element_to_be_clickable((By.CSS_SELECTOR, selector))).click()

    @staticmethod
    def button_path(label: str) -> tuple[str, str]:
        # Labels come from this file, not user input.
        return By.XPATH, f"//button[normalize-space(.)='{label}']"

    def button(self, label: str) -> WebElement:
        return self.wait.until(ec.visibility_of_element_located(self.button_path(label)))

    def press(self, label: str) -> None:
        self.wait.until(ec.element_to_be_clickable(self.button_path(label))).click()

    def disabled(self, label: str) -> None:
        self.wait.until(
            lambda _: not self.button(label).is_enabled(), f"{label} should be disabled."
        )

    def field(self, name: str, value: str) -> None:
        locator = (By.XPATH, f"//label[normalize-space(.)='{name}']/input")
        element = self.wait.until(ec.element_to_be_clickable(locator))
        element.clear()
        element.send_keys(value)

    def select_section(self, title: str) -> None:
        locator = (By.XPATH, f"//nav[@aria-label='Document sections']//button[strong='{title}']")
        self.wait.until(ec.element_to_be_clickable(locator)).click()
        self.text("#section-title", title)

    def status(self, title: str, status: str) -> None:
        locator = (By.XPATH, f"//nav[@aria-label='Document sections']//button[strong='{title}']")
        self.wait.until(
            ec.text_to_be_present_in_element(locator, status), f"{title}: expected {status}."
        )

    def seed(self, label: str, selected: str) -> None:
        self.press(label)
        self.text(".notice", f"{selected} selected.")

    def choose_snapshot(self, label: str) -> None:
        select = self.wait.until(ec.element_to_be_clickable((By.CSS_SELECTOR, ".toolbar select")))
        Select(select).select_by_visible_text(label)
        self.wait.until(
            lambda driver: (
                (picker := driver.find_element(By.CSS_SELECTOR, ".toolbar select")).is_enabled()
                and Select(picker).first_selected_option.text == label
            ),
            f"Could not select {label}.",
        )

    def draft(self, number: int = 1) -> None:
        self.press("Generate draft" if number == 1 else "Regenerate section")
        self.text(".notice", f"Revision {number} saved.")

    def approve(self) -> None:
        self.field("Reviewer name", "Selenium reviewer")
        self.press("Approve")
        self.text(".notice", "Revision approved.")

    def approve_sections(self, titles: tuple[str, ...] = SECTIONS) -> None:
        for title in titles:
            self.select_section(title)
            self.draft()
            self.approve()
            print(f"  [REVIEWED] {title} approved.")

    def source(self, index: int = 0) -> WebElement:
        matches = self.wait.until(
            lambda driver: driver.find_elements(*self.button_path("View source")),
        )
        matches[index].click()
        return self.visible(".evidence details[open]")

    def snapshot_id(self) -> str:
        return self.visible(".toolbar select").get_attribute("value")

    def request(self, path: str, body: dict | None = None) -> dict:
        return self.requests([(path, body)])[0]

    def requests(self, requests: list[tuple[str, dict | None]]) -> list[dict]:
        # Deliberate direct HTTP checks are still sent by the active Chrome page.
        result = self.driver.execute_async_script(
            """
            const [requests, done] = arguments;
            Promise.all(requests.map(async ([path, body]) => {
                const response = await fetch(path, body === null ? undefined : {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body)
                });
                return { status: response.status, data: await response.json() };
            })).then(done, error => done({ failure: String(error) }));
            """,
            requests,
        )
        if isinstance(result, dict):
            raise RuntimeError(result["failure"])
        return result

    def download_summary(self) -> dict:
        self.press("Export approved summary")
        self.text(".notice", "Approved summary exported.")
        file = self.downloads / "study-a101-approved-summary.json"
        self.wait.until(lambda _: file.is_file(), "The exported summary did not download.")
        return json.loads(file.read_text())


def trace_claim(browser: Browser, _: App, console: Console) -> None:
    browser.seed("Load baseline", "Baseline")
    browser.select_section("Week 12 efficacy")
    browser.draft()
    browser.text(".claims", "31/50 participants (62.0%)")
    browser.text(".claims", "20/50 participants (40.0%)")
    console.show("Chrome shows both Week 12 efficacy claims.", "Open the active-arm citation.")
    source = browser.source().text
    for field in ("efficacy.csv", "CSV line 2", "itt", "active", "week-12"):
        check(field in source, f"Active citation should show {field}.")
    console.observed(
        "The citation opens the exact CSV row with the correct population and time point."
    )


def export_approved(browser: Browser, _: App, console: Console) -> None:
    browser.seed("Load baseline", "Baseline")
    browser.approve_sections()
    console.show(
        "All four section approvals are visible.", "Download and validate the approved JSON."
    )
    document = browser.download_summary()
    check(len(document["sections"]) == 4, "Export should include four sections.")
    for entry in document["sections"]:
        revision = entry["revision"]
        check(
            revision["review"]["decision"] == "approve", "Every export revision must be approved."
        )
        evidence = {row["id"] for row in revision["draft"]["evidence"]}
        check(
            all(claim["evidence_id"] in evidence for claim in revision["draft"]["claims"]),
            "Every exported claim must link to included evidence.",
        )
    console.observed("The download contains all approvals and their evidence.")


def amendment(browser: Browser, _: App, console: Console) -> None:
    browser.seed("Load baseline", "Baseline")
    browser.approve_sections()
    browser.seed("Load amendment", "Amended safety")
    browser.status("Week 12 safety", "Source changed")
    for title in SECTIONS[:-1]:
        browser.status(title, "Approved")
    browser.disabled("Export approved summary")
    console.show(
        "Safety became stale. The other approvals still apply.",
        "Regenerate only safety, inspect both revisions, then approve the new one.",
        blocked=True,
    )
    browser.draft(2)
    browser.text(".claims", "6/49 participants (12.2%)")
    browser.click(".history > summary")
    browser.click(".history-item:last-child > summary")
    browser.text(".history-item:last-child", "5/49 participants (10.2%)")
    browser.text(".history-item:last-child", "safety-v1.csv")
    browser.approve()
    check(browser.button("Export approved summary").is_enabled(), "Export should be enabled again.")
    console.observed(
        "Safety revision 2 is approved. Revision 1 and its source are still visible in history."
    )


def requested_changes(browser: Browser, _: App, console: Console) -> None:
    browser.seed("Load baseline", "Baseline")
    browser.draft()
    browser.field("Reviewer name", "Selenium reviewer")
    browser.field("Review note", "Recheck the source wording.")
    browser.press("Request changes")
    browser.status("Study design", "Changes requested")
    console.show(
        "Revision 1 shows Changes requested.", "Regenerate and open the earlier review note."
    )
    browser.draft(2)
    browser.status("Study design", "Ready for review")
    browser.click(".history > summary")
    browser.click(".history-item:last-child > summary")
    browser.text(".history-item:last-child", "Recheck the source wording.")
    console.observed("Revision 2 is ready to review. The original note remains in history.")


def import_correction(browser: Browser, app: App, console: Console) -> None:
    app.import_b202("missing")
    browser.driver.refresh()
    browser.choose_snapshot(MISSING)
    browser.approve_sections(SECTIONS[:-1])
    browser.select_section("Week 12 safety")
    browser.draft()
    browser.status("Week 12 safety", "QC blocked")
    console.show(
        "B202 has three approvals and a blocked safety draft.",
        "Import the corrected source and regenerate the safety section.",
        blocked=True,
    )
    app.import_b202("corrected")
    browser.driver.refresh()
    browser.choose_snapshot(CORRECTED)
    browser.select_section("Week 12 safety")
    browser.status("Week 12 safety", "Source changed")
    browser.draft(2)
    browser.text(".claims", "5/69 participants (7.2%)")
    source = browser.source(1).text
    check("safety-v2.json" in source and "/facts/3" in source, "Expected the corrected JSON row.")
    console.show(
        "The placebo result now cites safety-v2.json at /facts/3.",
        "Approve the corrected revision to unlock export.",
    )
    browser.approve()
    check(
        browser.button("Export approved summary").is_enabled(),
        "Corrected B202 should be exportable.",
    )


def mobile_reload(browser: Browser, _: App, console: Console) -> None:
    driver = browser.driver
    driver.execute_cdp_cmd(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": 390,
            "height": 844,
            "deviceScaleFactor": 1,
            "mobile": False,
        },
    )
    try:
        browser.seed("Load baseline", "Baseline")
        browser.draft()
        browser.approve()
        driver.refresh()
        browser.status("Study design", "Approved")
        browser.click(".audit > summary")
        for event in ("snapshot ingested", "draft created", "review recorded"):
            browser.text(".audit", event)
        check(
            driver.execute_script("return document.documentElement.scrollWidth") == 390,
            "The mobile workspace should fit without horizontal overflow.",
        )
        console.show(
            "The approval and audit events survive reload at mobile width.",
            "Start the cases that are supposed to block unsafe actions.",
        )
    finally:
        driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})


def conflict(browser: Browser, _: App, console: Console) -> None:
    browser.seed("Load conflict", "Conflicting safety sources")
    browser.draft()
    browser.text(".findings", "conflicting evidence")
    browser.press("Source 1")
    browser.press("Source 2")
    check(
        len(browser.driver.find_elements(By.CSS_SELECTOR, ".evidence details[open]")) == 2,
        "Both conflicting sources should be open.",
    )
    browser.field("Reviewer name", "Selenium reviewer")
    browser.disabled("Approve")
    denied = browser.request(
        f"/api/snapshots/{browser.snapshot_id()}/sections/safety/review",
        {
            "revision": 1,
            "decision": "approve",
            "reviewer": "Selenium reviewer",
        },
    )
    check(
        denied["status"] == 409 and "Resolve the QC findings" in denied["data"]["detail"],
        "The API should reject an approval with conflicting evidence.",
    )
    console.show(
        "Two rows disagree about the same active-arm result. Approval returned HTTP 409.",
        "Choose the reconciled amendment, regenerate safety, and review its evidence.",
        blocked=True,
    )
    browser.seed("Load amendment", "Amended safety")
    browser.draft(2)
    browser.text(".claims", "6/49 participants (12.2%)")
    browser.approve()


def missing_evidence(browser: Browser, app: App, console: Console) -> None:
    app.import_b202("missing")
    browser.driver.refresh()
    browser.choose_snapshot(MISSING)
    browser.select_section("Week 12 safety")
    browser.draft()
    browser.text(".claims", "9/71 participants (12.7%)")
    check(
        "Placebo arm" not in browser.visible(".claims").text,
        "A missing result must not become a claim.",
    )
    browser.text(".findings", "safety · placebo · week-12")
    browser.field("Reviewer name", "Selenium reviewer")
    browser.disabled("Approve")
    browser.disabled("Export approved summary")
    console.show(
        "B202 has similar rows, but none match Week 12, placebo, and the safety population.",
        "Import the version containing the exact missing result, then regenerate and approve.",
        blocked=True,
    )
    app.import_b202("corrected")
    browser.driver.refresh()
    browser.choose_snapshot(CORRECTED)
    browser.select_section("Week 12 safety")
    browser.draft(2)
    browser.text(".claims", "5/69 participants (7.2%)")
    browser.approve()


def review_requirements(browser: Browser, _: App, console: Console) -> None:
    browser.seed("Load baseline", "Baseline")
    browser.draft()
    root = f"/api/snapshots/{browser.snapshot_id()}"
    browser.disabled("Export approved summary")
    check(
        browser.request(f"{root}/export")["status"] == 409,
        "Unreviewed export should return HTTP 409.",
    )
    browser.field("Reviewer name", "Selenium reviewer")
    browser.disabled("Request changes")
    denied = browser.request(
        f"{root}/sections/design/review",
        {
            "revision": 1,
            "decision": "request_changes",
            "reviewer": "Selenium reviewer",
        },
    )
    check(denied["status"] == 422, "Changes requested without a note should return HTTP 422.")
    console.show(
        "Export needs four approvals. Request changes needs a note.",
        "Enter a review note, regenerate the design, then approve all four sections.",
        blocked=True,
    )
    browser.field("Review note", "Verify the protocol wording.")
    browser.press("Request changes")
    browser.status("Study design", "Changes requested")
    browser.draft(2)
    browser.approve()
    browser.approve_sections(SECTIONS[1:])
    check(
        browser.button("Export approved summary").is_enabled(),
        "Approved report should be exportable.",
    )


def stale_review(browser: Browser, _: App, console: Console) -> None:
    browser.seed("Load baseline", "Baseline")
    browser.select_section("Week 12 safety")
    browser.draft()
    browser.seed("Load amendment", "Amended safety")
    path = f"/api/snapshots/{browser.snapshot_id()}/sections/safety/review"
    decision = {"revision": 1, "decision": "approve", "reviewer": "Selenium reviewer"}
    stale = browser.request(path, decision)
    check(
        stale["status"] == 409 and "Evidence changed" in stale["data"]["detail"],
        "Approval against stale evidence should return HTTP 409.",
    )
    console.show(
        "The approval refers to a draft with outdated evidence.",
        "Regenerate against the amendment and use the new revision number.",
        blocked=True,
    )
    browser.draft(2)
    superseded = browser.request(path, decision)
    check(
        superseded["status"] == 409 and "revision 2" in superseded["data"]["detail"],
        "Approval of the superseded revision should return HTTP 409.",
    )
    browser.approve()


def request_retries(browser: Browser, _: App, console: Console) -> None:
    browser.seed("Load baseline", "Baseline")
    root = f"/api/snapshots/{browser.snapshot_id()}/sections"
    request = {"expected_revision": 0, "request_id": "same-id"}
    first = browser.request(f"{root}/design/draft", request)
    replay = browser.request(f"{root}/design/draft", request)
    check(first["status"] == 200 and first == replay, "An exact retry must return the same draft.")
    collision = browser.request(f"{root}/disposition/draft", request)
    check(
        collision["status"] == 409, "Reusing the same ID for a different action must be rejected."
    )
    results = browser.requests(
        [
            (f"{root}/safety/draft", {"expected_revision": 0, "request_id": writer})
            for writer in ("writer-a", "writer-b")
        ]
    )
    check(
        sorted(row["status"] for row in results) == [200, 409],
        "Only one competing request should win.",
    )
    browser.press("Reload")
    browser.select_section("Week 12 safety")
    browser.text(".draft .eyebrow", "REVISION 1")
    console.show(
        "An exact retry reused its revision. A colliding ID and a competing write returned HTTP 409.",
        "Use the reloaded revision and a new request ID. Regenerate in Chrome to see revision 2.",
        blocked=True,
    )
    browser.draft(2)


def cross_origin(browser: Browser, app: App, console: Console) -> None:
    driver = browser.driver
    driver.get(app.url.replace("127.0.0.1", "localhost"))
    driver.get_log("performance")
    target = app.url + "/api/scenarios/baseline"
    error = driver.execute_async_script(
        """
        const [url, done] = arguments;
        fetch(url, { method: "POST", mode: "no-cors" }).then(() => done(null), e => done(String(e)));
        """,
        target,
    )
    check(error is None, f"Chrome should send the cross-origin request: {error}.")

    def denied_response(_: webdriver.Chrome) -> bool:
        # JavaScript cannot read the no-cors response. Chrome's network event can.
        for entry in driver.get_log("performance"):
            event = json.loads(entry["message"])["message"]
            response = event.get("params", {}).get("response", {})
            if response.get("url") == target and response.get("status") == 403:
                return True
        return False

    browser.wait.until(denied_response, "Chrome did not record the HTTP 403 response.")
    check(
        browser.request("/api/snapshots")["data"] == [],
        "The blocked write must not create a snapshot.",
    )
    console.show(
        "Chrome recorded HTTP 403 for a write sent from a different origin.",
        "Return to the app's own origin and load the baseline there.",
        blocked=True,
    )
    driver.get(app.url)
    browser.seed("Load baseline", "Baseline")


CASES: tuple[tuple[str, str, Callable[[Browser, App, Console], None]], ...] = (
    ("HAPPY", "Trace an efficacy claim", trace_claim),
    ("HAPPY", "Approve and export all four sections", export_approved),
    ("HAPPY", "Regenerate safety after an amendment", amendment),
    ("HAPPY", "Request changes and preserve review history", requested_changes),
    ("HAPPY", "Correct B202 and restore export", import_correction),
    ("HAPPY", "Reload mobile view and audit log", mobile_reload),
    ("GUARD", "Block conflicting evidence", conflict),
    ("GUARD", "Block missing evidence", missing_evidence),
    ("GUARD", "Require approvals and review notes", review_requirements),
    ("GUARD", "Reject stale approvals", stale_review),
    ("GUARD", "Handle retries and competing drafts", request_retries),
    ("GUARD", "Reject cross-origin writes", cross_origin),
)


def run(console: Console, headless: bool) -> int:
    passed = 0
    failures = []
    with TemporaryDirectory(prefix="study-desk-downloads-") as directory:
        options = webdriver.ChromeOptions()
        options.add_argument("--window-size=1400,1000")
        if headless:
            options.add_argument("--headless=new")
        options.add_experimental_option(
            "prefs",
            {
                "download.default_directory": directory,
                "download.prompt_for_download": False,
            },
        )
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        print(
            "Opening a Selenium-controlled Chrome window."
            if not headless
            else "Starting headless Chrome."
        )
        with webdriver.Chrome(options=options) as driver:
            driver.set_page_load_timeout(15)
            driver.set_script_timeout(12)
            browser = Browser(driver, Path(directory))
            if console.interactive:
                print(
                    "Keep Terminal and Chrome visible. Press Enter in Terminal at each checkpoint."
                )
            print(
                f"Chrome {driver.capabilities['browserVersion']}. Each case starts with a fresh workspace."
            )
            try:
                for number, (kind, title, scenario) in enumerate(CASES, 1):
                    print(f"\n[{number:02}/{len(CASES)}] {kind} · {title}")
                    try:
                        with fresh_app() as app:
                            driver.get(app.url)
                            scenario(browser, app, console)
                            passed += 1
                            print(f"  [PASS] {title}")
                            if console.interactive and number < len(CASES):
                                print("  [NEXT] Inspect this result, then start the next case.")
                                console.pause()
                    except (AssertionError, OSError, RuntimeError, WebDriverException) as exc:
                        failures.append(title)
                        print(
                            f"  [FAIL] {title}\n         {str(exc).split('Stacktrace:')[0].strip()}"
                        )
                        console.show(
                            "An assertion or browser operation failed.",
                            "Inspect the current page, then continue to the next isolated case.",
                        )
            except StopWalkthrough:
                print(f"\nStopped by viewer after {passed + len(failures)} completed cases.")
                return 130

            print(f"\nSelenium summary: {passed}/{len(CASES)} passed, {len(failures)} failed.")
            for title in failures:
                print(f"  [FAIL] {title}")
            if console.interactive:
                try:
                    console.pause()
                except StopWalkthrough:
                    pass
    return int(bool(failures))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auto", action="store_true", help="run continuously with visible Chrome")
    parser.add_argument(
        "--headless", action="store_true", help="run automatically without opening a window"
    )
    args = parser.parse_args()
    interactive = not (args.auto or args.headless)
    if interactive and not sys.stdin.isatty():
        parser.error(
            "The guided mode needs a terminal. Use --auto or --headless for unattended runs."
        )
    try:
        raise SystemExit(run(Console(interactive), args.headless))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except WebDriverException as exc:
        parser.exit(1, f"Could not start Selenium Chrome: {exc.msg}\n")


if __name__ == "__main__":
    main()
