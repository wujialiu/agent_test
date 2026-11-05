// Keep the browser's data contract close to the routes it calls.
export type Snapshot = { id: string; study: string; label: string };
export type Evidence = {
  id: string;
  locator: string;
  excerpt: string;
  source: { document_id: string; filename: string; version: string; sha256: string };
  key: { metric: string; population: string; arm: string; timepoint: string };
  fact: { value: string; unit: string; denominator: number | null };
};
export type Finding = { code: string; slot_id: string; message: string; evidence_ids: string[] };
export type Review = {
  decision: "approve" | "request_changes";
  reviewer: string;
  note: string;
  created_at: string;
};
export type Revision = {
  number: number;
  created_at: string;
  snapshot_id: string;
  review: Review | null;
  draft: {
    generator: string;
    claims: { slot_id: string; sentence: string; evidence_id: string }[];
    findings: Finding[];
    evidence: Evidence[];
  };
};
export type Section = {
  id: string;
  title: string;
  status: "not_drafted" | "stale" | "blocked" | "approved" | "pending_review" | "changes_requested";
  revision: Revision | null;
};
export type Workspace = { snapshot: Snapshot; sections: Section[]; export_ready: boolean };
export type AuditEvent = { id: number; event: string; created_at: string; details: Record<string, unknown> };

export async function request<T>(path: string, body?: object): Promise<T> {
  const options = body === undefined
    ? undefined
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const response = await fetch(`/api${path}`, options);
  if (!response.ok) {
    const data: { detail?: unknown } = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (HTTP ${response.status}).`);
  }
  return response.json() as Promise<T>;
}

export function sectionPath(snapshot: string, section: string) {
  return `/snapshots/${snapshot}/sections/${section}`;
}
