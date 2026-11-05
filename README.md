# Study desk

A local document-agent portfolio project inspired by Artos AI’s public workflow for life sciences teams. It takes a **fictional clinical study** from structured source files through drafting, source inspection, quality checks, review, and export. The Python backend owns the workflow and review decisions. The React and TypeScript interface lets a reviewer inspect those decisions. [Artos’s description of its agent architecture](https://www.artosai.com/blog/how-agentic-systems-help-enterprises-scale)

## Run the project

Run these commands from the repository root. The project was tested on macOS with Python 3.13 and Node 26.

```zsh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-artos.txt
npm --prefix artos_document_agent_py/web ci
npm --prefix artos_document_agent_py/web run build
.venv/bin/python -m artos_document_agent_py serve
```

Open [the reviewer workspace](http://127.0.0.1:8765/) or [the FastAPI docs](http://127.0.0.1:8765/docs). The workspace stores local state in `.agent_lab/artos/workspace.sqlite3`. To use a separate database, pass `--db /path/to/workspace.sqlite3` before `serve`.

For a quick check without Node or a browser, run the scripted demo. It uses its own temporary database and simulated reviewer decisions.

```zsh
.venv/bin/python -m artos_document_agent_py demo
```

## Interview demo

1. Click **Load baseline**. Generate **Week 12 efficacy** and open **View source** beside the active-arm claim. The interface shows the exact CSV row, source version, content hash, study population, and time point. The fixture also has a Week 4 row and a per-protocol row, so the demo can show why scope matters.
2. Generate the other sections and approve all four under a reviewer name. **Export approved summary** downloads JSON with the approved revisions, review decisions, claims, and supporting source records.
3. Click **Load amendment**. The Week 12 safety section becomes stale. Regenerate it and compare the active-arm value of **6/49** with **5/49** in revision history. The other three approvals still apply because their source evidence is identical. Review the new safety revision to make export available again.
4. Click **Load conflict** and regenerate safety. Two records disagree about the same active-arm count. Open both sources under **Quality findings** and confirm that **Approve** is disabled. The API also rejects an approval or export that bypasses the interface.
5. Open **Study audit log** to see ingestion, draft creation, and review events.

[Artos Source Tracer](https://www.artosai.com/feature/generative-ai-medical-writing-source-tracer) · [Artos Section Regeneration](https://www.artosai.com/feature/generative-ai-medical-writing-document-section-regeneration)

## Read the code

| Area | What to follow |
|---|---|
| [Ingestion](artos_document_agent_py/ingest_py.py) | Import the selected source versions, validate the study on each record, and assign a content hash and exact source location. |
| [Domain and template](artos_document_agent_py/domain_py.py) | Four sections define the facts they require. Each fact is scoped by metric, population, arm, and time point within one study. |
| [LangGraph workflow](artos_document_agent_py/workflow_py.py) | Retrieve exact matches, request structured claim proposals, check them against the original values, and compute percentages in application code. |
| [Storage and review](artos_document_agent_py/storage_py.py) | Keep snapshots and revisions in SQLite. Compare the caller’s expected revision inside the write transaction. Reuse the existing revision when a request ID is replayed. |
| [Application service](artos_document_agent_py/service_py.py) | Detect stale evidence and require current approvals before export. |
| [FastAPI](artos_document_agent_py/api_py.py) and [React](artos_document_agent_py/web/src/main.tsx) | Follow the same workflow from the HTTP routes through the reviewer interface. |

The agent uses a deterministic proposer by default. For an optional live model, set `OPENAI_API_KEY` and `OPENAI_MODEL` for a compatible model and run `serve --live`. This sends candidate fact rows to OpenAI for structured proposals. Use synthetic or authorized data. Both proposers run through the same checks, and the reviewer still makes the approval decision. [OpenAI Structured Outputs documentation](https://developers.openai.com/api/docs/guides/structured-outputs)

## Import a synthetic study

Start with the [B202 practice data](artos_document_agent_py/practice_data/README.md) to see a missing Week 12 result block approval, then import the corrected source.

Copy one of the [sample manifests](artos_document_agent_py/fixtures/baseline.json) and its source files to your own directory. The manifest identifies the study and names one version of each document. A CSV file uses these columns:

```text
study,metric,population,arm,timepoint,value,unit,denominator
```

The [protocol example](artos_document_agent_py/fixtures/protocol.json) shows the same fields as JSON. File names are relative to the manifest directory. Increment the document version when its contents change. Import the manifest into the database your server uses, then reload the workspace.

```zsh
.venv/bin/python -m artos_document_agent_py ingest /path/to/manifest.json
```

This is a single-user interview prototype with a fixed summary template, structured CSV/JSON inputs, and JSON export. The reviewer name is a local label. A regulated deployment would require verified identities and roles, validated parsers and workflows, and stronger audit controls.

## Verify and practice

```zsh
.venv/bin/python -m unittest discover -q
```

To run the reviewer workflows in installed Google Chrome, use the commands below. Each test starts the app on a temporary port with a fresh SQLite database. The report groups successful workflows under **Happy paths** and checks of safety gates under **Expected blocks**. Open a case to see its steps and any attached screenshots or traces.

```zsh
npm --prefix artos_document_agent_py/web run test:chrome
npm --prefix artos_document_agent_py/web run test:report
```

Add `-- --headed` to the first command to watch Chrome run the tests. The report is also saved to `.agent_lab/browser/report/index.html`.

### Watch the workflow with Selenium

From the repository root, install the Selenium dependencies once, then run the shell script.

```zsh
.venv/bin/python -m pip install -r requirements-selenium.txt
./run_selenium.sh
```

Keep Terminal and the Selenium-controlled Chrome window side by side. The script fills the forms and stops at checkpoints. When an action is blocked, **EXPECTED BLOCK** explains what Chrome observed and **NEXT** tells you how the script will resolve it. Press Enter in Terminal to see that action, or type `q` to quit. Each case uses its own temporary database and port.

Use `./run_selenium.sh --auto` to watch all 12 cases without pausing, or `./run_selenium.sh --headless` for a terminal-only run. The final summary distinguishes expected product blocks from Selenium failures. Selenium Manager may download a compatible ChromeDriver on the first run. [Selenium Manager documentation](https://www.selenium.dev/documentation/selenium_manager/)

Explain why a valid citation can still support the wrong number. Then point to the test that changes a count while keeping the citation ID. For the amendment exercise, explain why regenerating one section requires another review while unaffected approvals survive. For concurrency, trace the test where two drafts start from the same revision and only one is saved.

The original three-stage exercises are in [Learning Labs](LEARNING_LABS.md).
