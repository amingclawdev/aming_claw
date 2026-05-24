import { useMemo, useState } from "react";
import type {
  AssetInboxBatchAction,
  AssetInboxItem,
  AssetInboxResponse,
  AssetInboxStatus,
} from "../types";

interface Props {
  assetInbox: AssetInboxResponse;
  projectId: string;
  snapshotId: string;
}

type StatusFilter = AssetInboxStatus | "ALL";

const STATUS_ORDER: StatusFilter[] = [
  "ALL",
  "source_orphan",
  "doc_unbound",
  "doc_candidate",
  "test_candidate",
  "config_pending_decision",
  "stale",
  "accepted",
  "ignored",
  "archive",
];

const STATUS_LABELS: Record<string, string> = {
  source_orphan: "Source orphan",
  doc_unbound: "Doc unbound",
  doc_candidate: "Doc candidate",
  accepted: "Accepted",
  test_candidate: "Test candidate",
  config_pending_decision: "Config pending",
  ignored: "Ignored",
  archive: "Archive",
  stale: "Stale",
};

export default function AssetInboxView({ assetInbox, projectId, snapshotId }: Props) {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("ALL");
  const [query, setQuery] = useState("");
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (assetInbox.items ?? [])
      .filter((item) => statusFilter === "ALL" || item.asset_status === statusFilter)
      .filter((item) => {
        if (!q) return true;
        const hay = [
          item.path,
          item.asset_kind,
          item.asset_status,
          item.graph_status,
          item.scan_status,
          ...item.accepted_bindings.map((binding) => `${binding.node_id} ${binding.title ?? ""}`),
          ...item.binding_candidates.map((candidate) => `${candidate.target_node_id} ${candidate.target_title ?? ""}`),
        ]
          .join(" ")
          .toLowerCase();
        return hay.includes(q);
      })
      .slice()
      .sort(compareAssets);
  }, [assetInbox.items, query, statusFilter]);
  const statusCounts = assetInbox.summary?.by_status ?? {};
  const reviewCount = assetInbox.summary?.operator_review_count ?? 0;
  const backlogEligible = assetInbox.summary?.backlog_eligible_count ?? 0;

  return (
    <div className="view">
      <div className="view-head">
        <h2 className="view-title">Asset Inbox</h2>
        <span className="view-subtitle">
          source <span className="mono">/api/graph-governance/{projectId}/snapshots/{snapshotId}/asset-inbox</span> ·{" "}
          {rows.length} shown · {assetInbox.summary.total} total
        </span>
      </div>

      <div className="backlog-guidance">
        <div>
          <strong>File and graph hygiene.</strong> Weak candidates stay reviewable; accepted bindings are the only assets used for impact scope.
        </div>
        <span className="mono">backlog from explicit selection only</span>
      </div>

      <div className="score-grid backlog-score-grid">
        <Kpi label="Review" value={reviewCount} tone={reviewCount > 0 ? "amber" : "green"} />
        <Kpi label="Backlog eligible" value={backlogEligible} tone={backlogEligible > 0 ? "red" : "neutral"} />
        <Kpi label="Accepted" value={assetInbox.summary.accepted_count ?? countStatus(assetInbox, "accepted")} tone="green" />
        <Kpi label="Total" value={assetInbox.summary.total} tone="blue" />
      </div>

      <div className="asset-inbox-toolbar card">
        <div className="backlog-filter-group">
          {STATUS_ORDER.map((status) => (
            <button
              key={status}
              className={`chip ${statusFilter === status ? "on" : "off"}`}
              onClick={() => setStatusFilter(status)}
              title={status === "ALL" ? "All asset states" : status}
            >
              {status === "ALL" ? "All" : STATUS_LABELS[status] ?? status}
              <span className="asset-chip-count">
                {status === "ALL" ? assetInbox.summary.total : statusCounts[status] ?? 0}
              </span>
            </button>
          ))}
        </div>
        <input
          className="backlog-search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search assets, nodes, evidence..."
        />
      </div>

      <div className="section">
        <div className="section-head">
          Batch actions <span className="head-hint">read-only preview in this slice</span>
        </div>
        <div className="asset-action-grid">
          {(assetInbox.batch_actions ?? []).map((action) => (
            <ActionCard key={action.action} action={action} />
          ))}
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          Assets <span className="head-hint">{rows.length} rows, sorted by state and path</span>
        </div>
        {rows.length === 0 ? (
          <div className="empty">No assets match the current filters.</div>
        ) : (
          <div className="card asset-inbox-table-card">
            <table className="table asset-inbox-table">
              <thead>
                <tr>
                  <th style={{ width: 156 }}>State</th>
                  <th>Asset</th>
                  <th style={{ width: 260 }}>Binding</th>
                  <th style={{ width: 280 }}>Evidence</th>
                  <th style={{ width: 150 }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((item) => (
                  <AssetRow key={item.asset_id} item={item} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function AssetRow({ item }: { item: AssetInboxItem }) {
  return (
    <tr>
      <td>
        <span className={`status-badge ${assetStatusClass(item.asset_status)}`}>
          {STATUS_LABELS[item.asset_status] ?? item.asset_status}
        </span>
        <div className="asset-risk-line">{item.risk || "risk unknown"}</div>
      </td>
      <td>
        <div className="cell-strong mono">{item.path}</div>
        <div className="cell-mono-id">
          {item.asset_kind} · {item.scan_status || "scan n/a"} · {item.graph_status || "graph n/a"}
        </div>
      </td>
      <td>
        <BindingSummary item={item} />
      </td>
      <td>
        <div className="asset-evidence-list">
          {(item.evidence ?? []).slice(0, 2).map((evidence, index) => (
            <span key={`${item.asset_id}-e-${index}`}>
              <strong>{evidence.kind}</strong>: {evidence.message}
            </span>
          ))}
        </div>
      </td>
      <td>
        <div className="asset-action-list">
          {(item.recommended_actions ?? []).slice(0, 3).map((action) => (
            <span key={action} className="mono">
              {action}
            </span>
          ))}
          {item.recommended_actions.length === 0 ? <span className="muted">No action</span> : null}
        </div>
      </td>
    </tr>
  );
}

function BindingSummary({ item }: { item: AssetInboxItem }) {
  if (item.accepted_bindings.length > 0) {
    return (
      <div className="asset-binding-list">
        {item.accepted_bindings.slice(0, 3).map((binding) => (
          <span key={`${item.asset_id}-${binding.node_id}`} className="asset-binding accepted">
            {binding.role} {binding.node_id}
            {binding.title ? <em>{binding.title}</em> : null}
          </span>
        ))}
      </div>
    );
  }
  if (item.binding_candidates.length > 0) {
    return (
      <div className="asset-binding-list">
        {item.binding_candidates.slice(0, 3).map((candidate) => (
          <span key={`${item.asset_id}-${candidate.proposal_hash}`} className="asset-binding candidate">
            weak {candidate.target_node_id}
            {candidate.target_title ? <em>{candidate.target_title}</em> : null}
          </span>
        ))}
      </div>
    );
  }
  return <span className="muted">No binding</span>;
}

function ActionCard({ action }: { action: AssetInboxBatchAction }) {
  return (
    <div className="asset-action-card card">
      <div className="asset-action-head">
        <span>{action.label || action.action}</span>
        <span className={action.mutates_source ? "asset-action-danger" : "asset-action-safe"}>
          {action.mutates_source ? "source write" : "read/queue"}
        </span>
      </div>
      <div className="asset-action-meta">
        {(action.allowed_statuses ?? []).map((status) => (
          <span key={status}>{STATUS_LABELS[status] ?? status}</span>
        ))}
      </div>
      <button className="action-btn" disabled title="Mutation actions are not enabled in this slice">
        Disabled
      </button>
    </div>
  );
}

function Kpi({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className={`score-card tone-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function compareAssets(a: AssetInboxItem, b: AssetInboxItem): number {
  const byState = statusWeight(a.asset_status) - statusWeight(b.asset_status);
  if (byState !== 0) return byState;
  return a.path.localeCompare(b.path);
}

function statusWeight(status: string): number {
  const index = STATUS_ORDER.indexOf(status as StatusFilter);
  return index < 0 ? 99 : index;
}

function assetStatusClass(status: string): string {
  if (status === "accepted") return "qa";
  if (status === "ignored" || status === "archive") return "muted";
  if (status === "source_orphan" || status === "stale") return "failed";
  if (status === "doc_candidate" || status === "test_candidate" || status === "config_pending_decision") return "running";
  return "queued";
}

function countStatus(assetInbox: AssetInboxResponse, status: string): number {
  return assetInbox.summary.by_status?.[status] ?? 0;
}
