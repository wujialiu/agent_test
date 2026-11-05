# Practice study B202

All records in this folder are fictional aggregate data. The exercise tests how a clinical writing workflow handles a missing result when similar source rows are available.

Run the commands from the repository root. They use the same default database as the browser workspace. If you started the server with a custom database, pass the same `--db` option before `ingest`.

## 1. Load the incomplete study

```zsh
.venv/bin/python -m artos_document_agent_py ingest \
  artos_document_agent_py/practice_data/b202-missing.json
```

Refresh the browser page and select **study-b202 · B202 missing Week 12 safety** under **Source snapshot**. Generate and review the design, enrollment, and efficacy sections. Generate **Week 12 safety** and inspect the quality finding.

| Check | Expected result |
|---|---|
| Enrollment | 72 active and 70 placebo |
| Week 12 efficacy, intent-to-treat (ITT) | Active 39/72 (54.2%) and placebo 28/70 (40.0%) |
| Week 12 safety, safety population | Active 9/71 (12.7%). The required placebo result is missing. |
| Approval | Safety approval and report export are blocked. |

Open [safety-v1.json](safety-v1.json) and compare the two placebo records. One uses Week 4 and the safety population. The other uses Week 12 and the ITT population. The required row has both Week 12 and the safety population.

## 2. Import the corrected source

```zsh
.venv/bin/python -m artos_document_agent_py ingest \
  artos_document_agent_py/practice_data/b202-corrected.json
```

Refresh the browser and select **study-b202 · B202 corrected Week 12 safety**. Only safety should show **Source changed**. Regenerate safety and check the placebo claim. It should read **5/69 (7.2%)** and link to `safety-v2.json` at `/facts/3`. The old blocked draft remains in revision history.

Approve the new safety revision. If you approved the other three sections in step 1, **Export approved summary** should now be available.
