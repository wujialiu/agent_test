import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  request, sectionPath,
  type AuditEvent, type Evidence, type Revision, type Review, type Section, type Snapshot, type Workspace,
} from "./api";
import "./style.css";

const LABELS: Record<Section["status"], string> = {
  not_drafted: "Not drafted",
  stale: "Source changed",
  blocked: "QC blocked",
  approved: "Approved",
  pending_review: "Ready for review",
  changes_requested: "Changes requested",
};

const SCENARIOS = [
  { id: "baseline", title: "Load baseline" },
  { id: "amended", title: "Load amendment" },
  { id: "conflict", title: "Load conflict" },
];

function SourceCard({ evidence, prefix = "source" }: { evidence: Evidence; prefix?: string }) {
  return (
    <details id={`${prefix}-${evidence.id}`} className="source">
      <summary>
        <span><strong>{evidence.source.filename}</strong> <span className="version">{evidence.source.version}</span></span>
        <span className="locator">{evidence.locator}</span>
      </summary>
      <dl>
        <div><dt>Population</dt><dd>{evidence.key.population}</dd></div>
        <div><dt>Arm</dt><dd>{evidence.key.arm}</dd></div>
        <div><dt>Time point</dt><dd>{evidence.key.timepoint}</dd></div>
      </dl>
      <pre>{evidence.excerpt}</pre>
      <p className="digest">SHA-256 {evidence.source.sha256}</p>
    </details>
  );
}

function focusEvidence(id: string) {
  const source = document.getElementById(`source-${id}`);
  if (source instanceof HTMLDetailsElement) {
    source.open = true;
    source.querySelector("summary")?.focus();
    source.scrollIntoView({ block: "nearest" });
  }
}

function RevisionHistory({ revisions }: { revisions: Revision[] }) {
  return (
    <details className="history">
      <summary>Revision history <span>{revisions.length}</span></summary>
      {revisions.length === 0 && <p>No revisions yet.</p>}
      {revisions.map((revision) => (
        <details key={revision.number} className="history-item">
          <summary>
            <strong>Revision {revision.number}</strong>
            <span>{revision.review?.decision.replaceAll("_", " ") ?? "Unreviewed"}</span>
          </summary>
          {revision.draft.claims.map((claim) => <p key={claim.slot_id}>{claim.sentence}</p>)}
          {revision.draft.findings.map((finding) => (
            <p key={`${finding.slot_id}-${finding.code}`}>{finding.message}</p>
          ))}
          {revision.review && <p>Reviewed by {revision.review.reviewer}. {revision.review.note}</p>}
          <p className="digest">Snapshot {revision.snapshot_id.slice(0, 12)} · {revision.created_at}</p>
          {revision.draft.evidence.map((evidence) => (
            <SourceCard key={evidence.id} evidence={evidence} prefix={`revision-${revision.number}`} />
          ))}
        </details>
      ))}
    </details>
  );
}

function App() {
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [sectionId, setSectionId] = useState("design");
  const [history, setHistory] = useState<Revision[]>([]);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [reviewer, setReviewer] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const section = workspace?.sections.find((entry) => entry.id === sectionId);
  const revision = section?.revision;

  // Fetch the current document and history as a unit so a snapshot change
  // cannot leave a section showing evidence from a different selection.
  async function openSnapshot(snapshot: string, selected = sectionId) {
    const [document, revisions, events] = await Promise.all([
      request<Workspace>(`/snapshots/${snapshot}/workspace`),
      request<Revision[]>(`${sectionPath(snapshot, selected)}/history`),
      request<AuditEvent[]>(`/snapshots/${snapshot}/audit`),
    ]);
    setWorkspace(document);
    setSectionId(selected);
    setHistory(revisions);
    setAudit(events);
  }

  async function act(operation: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await operation();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Request failed.");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void act(async () => {
      const choices = await request<Snapshot[]>("/snapshots");
      setSnapshots(choices);
      if (choices.length) await openSnapshot(choices[0].id, "design");
    });
  }, []);

  function seed(id: string) {
    void act(async () => {
      const loaded = await request<Snapshot>(`/scenarios/${id}`, {});
      setSnapshots(await request<Snapshot[]>("/snapshots"));
      await openSnapshot(loaded.id, id === "baseline" ? "design" : "safety");
      setNote("");
      setNotice(`${loaded.label} selected.`);
    });
  }

  function selectSection(id: string) {
    if (!workspace) return;
    const snapshot = workspace.snapshot.id;
    void act(async () => {
      setHistory(await request<Revision[]>(`${sectionPath(snapshot, id)}/history`));
      setSectionId(id);
      setNote("");
    });
  }

  function generate() {
    if (!workspace || !section) return;
    const snapshot = workspace.snapshot.id;
    void act(async () => {
      const saved = await request<Revision>(`${sectionPath(snapshot, section.id)}/draft`, {
        expected_revision: section.revision?.number ?? 0,
        request_id: crypto.randomUUID(),
      });
      await openSnapshot(snapshot);
      setNotice(`Revision ${saved.number} saved.`);
      setNote("");
    });
  }

  function review(decision: Review["decision"]) {
    if (!workspace || !section || !revision) return;
    const snapshot = workspace.snapshot.id;
    void act(async () => {
      await request<Revision>(`${sectionPath(snapshot, section.id)}/review`, {
        revision: revision.number, decision, reviewer, note,
      });
      await openSnapshot(snapshot);
      setNotice(decision === "approve" ? "Revision approved." : "Changes requested.");
      setNote("");
    });
  }

  function exportDraft() {
    if (!workspace) return;
    const snapshot = workspace.snapshot;
    void act(async () => {
      const document = await request<Workspace>(`/snapshots/${snapshot.id}/export`);
      const file = new Blob([JSON.stringify(document, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(file);
      const link = documentNode(url, `${snapshot.study}-approved-summary.json`);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice("Approved summary exported.");
    });
  }

  return (
    <>
      <header className="top">
        <div className="top-inner">
          <span className="mark" aria-hidden="true">S</span>
          <div><strong>Study desk</strong><span className="top-subtitle">Document review lab</span></div>
          <span className="synthetic">Synthetic study data</span>
        </div>
      </header>
      <main>
        <section className="intro" aria-label="Study setup">
          <p className="eyebrow">Clinical document workflow</p>
          <h1>Study summary review</h1>
          <p>Generate a section, inspect the source rows, and record a review decision.</p>
          <div className="scenarios">
            {SCENARIOS.map((item) => (
              <button key={item.id} disabled={busy} onClick={() => seed(item.id)}>{item.title}</button>
            ))}
          </div>
        </section>
        <div className="notifications" aria-live="polite">
          {busy && <p>Working…</p>}
          {notice && <p className="notice">{notice}</p>}
          {error && <p className="error" role="alert">{error} Reload the workspace if the revision changed.</p>}
        </div>
        {workspace && section ? (
          <>
            <div className="toolbar">
              <label>Source snapshot
                <select value={workspace.snapshot.id} disabled={busy} onChange={(event) => {
                  const selected = event.currentTarget.value;
                  void act(async () => { await openSnapshot(selected); setNote(""); });
                }}>
                  {snapshots.map((snapshot) => (
                    <option key={snapshot.id} value={snapshot.id}>{snapshot.study} · {snapshot.label}</option>
                  ))}
                </select>
              </label>
              <div className="toolbar-actions">
                <button disabled={busy} onClick={() => void act(() => openSnapshot(workspace.snapshot.id))}>
                  Reload
                </button>
                <button disabled={busy || !workspace.export_ready} onClick={exportDraft}>
                  Export approved summary
                </button>
              </div>
            </div>
            <div className="desk">
              <nav className="sections panel" aria-label="Document sections">
                <h2>Sections</h2>
                {workspace.sections.map((entry) => (
                  <button key={entry.id} disabled={busy} aria-current={entry.id === sectionId ? "true" : undefined}
                    onClick={() => selectSection(entry.id)}>
                    <strong>{entry.title}</strong>
                    <span className={`status ${entry.status}`}>{LABELS[entry.status]}</span>
                  </button>
                ))}
              </nav>
              <section className="draft panel" aria-labelledby="section-title">
                <div className="heading-row">
                  <div>
                    <p className="eyebrow">{revision ? `Revision ${revision.number}` : "New section"}</p>
                    <h2 id="section-title">{section.title}</h2>
                  </div>
                  <button className="primary" disabled={busy} onClick={generate}>
                    {revision ? "Regenerate section" : "Generate draft"}
                  </button>
                </div>
                {section.status === "stale" && <p className="warning">
                  This draft uses evidence from a different source revision. Regenerate before approval.
                </p>}
                {revision ? (
                  <>
                    <div className="claims">
                      {revision.draft.claims.map((claim) => (
                        <p key={claim.slot_id}>{claim.sentence}{" "}
                          <button className="citation" onClick={() => focusEvidence(claim.evidence_id)}>
                            View source
                          </button>
                        </p>
                      ))}
                      {!revision.draft.claims.length && <p>No claims passed the evidence checks.</p>}
                    </div>
                    {revision.draft.findings.length > 0 && (
                      <div className="findings">
                        <h3>Quality findings</h3>
                        {revision.draft.findings.map((finding) => (
                          <div key={`${finding.slot_id}-${finding.code}`}>
                            <strong>{finding.slot_id} · {finding.code.replaceAll("_", " ")}</strong>
                            <p>{finding.message}</p>
                            {finding.evidence_ids.map((id, index) => (
                              <button key={id} className="citation" onClick={() => focusEvidence(id)}>Source {index + 1}</button>
                            ))}
                          </div>
                        ))}
                      </div>
                    )}
                    <div className="review">
                      <h3>Review decision</h3>
                      {revision.review ? (
                        <p>{revision.review.decision === "approve" ? "Approved" : "Changes requested"} by{" "}
                          <strong>{revision.review.reviewer}</strong>. {revision.review.note}</p>
                      ) : (
                        <>
                          <div className="review-fields">
                            <label>Reviewer name
                              <input maxLength={400} value={reviewer} onChange={(event) => setReviewer(event.target.value)} />
                            </label>
                            <label>Review note
                              <input maxLength={800} value={note} onChange={(event) => setNote(event.target.value)} />
                            </label>
                          </div>
                          <div className="review-buttons">
                            <button className="primary" onClick={() => review("approve")}
                              disabled={busy || !reviewer.trim() || section.status !== "pending_review"}>Approve</button>
                            <button onClick={() => review("request_changes")}
                              disabled={busy || !reviewer.trim() || !note.trim() || section.status === "stale"}>Request changes</button>
                          </div>
                        </>
                      )}
                    </div>
                  </>
                ) : <p>Generate a draft to see claims and source references here.</p>}
                <RevisionHistory revisions={history} />
              </section>
              <aside className="evidence panel" aria-labelledby="evidence-title">
                <p className="eyebrow">Traceability</p>
                <h2 id="evidence-title">Evidence</h2>
                <p>Expand a source to inspect the original record and its version.</p>
                {revision?.draft.evidence.map((evidence) => <SourceCard key={evidence.id} evidence={evidence} />)}
                {!revision?.draft.evidence.length && <p>No evidence in this revision.</p>}
              </aside>
            </div>
            <details className="audit panel">
              <summary>Study audit log <span>{audit.length} events</span></summary>
              <ol>
                {audit.map((entry) => (
                  <li key={entry.id}>
                    <strong>{entry.event.replaceAll("_", " ")}</strong>{" "}
                    <time dateTime={entry.created_at}>{entry.created_at}</time>
                    <pre>{JSON.stringify(entry.details, null, 2)}</pre>
                  </li>
                ))}
              </ol>
            </details>
          </>
        ) : <section className="panel welcome"><h2>Start with the baseline</h2><p>Load the fictional study to begin drafting.</p></section>}
      </main>
    </>
  );
}

function documentNode(url: string, filename: string) {
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  return link;
}

const root = document.getElementById("root");
if (!root) throw new Error("Missing application root.");
createRoot(root).render(<App />);
