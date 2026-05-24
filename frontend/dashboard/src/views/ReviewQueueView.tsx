import { Fragment, useMemo, useState } from "react";
import type {
  AssetImpactReminder,
  AssetImpactReminderEventsResponse,
  AssetImpactRemindersResponse,
  AssetImpactResolutionKind,
  FeedbackActionCatalog,
  FeedbackQueueGroup,
  FeedbackQueueResponse,
} from "../types";
import RetryFeedbackModal from "../components/RetryFeedbackModal";

interface Props {
  feedback: FeedbackQueueResponse;
  assetImpactReminders?: AssetImpactRemindersResponse;
  assetImpactReminderEvents?: Record<string, AssetImpactReminderEventsResponse>;
  assetImpactBusyId?: string | null;
  assetImpactError?: string | null;
  onDecide?: (feedbackIds: string[], action: string, summaryHint?: string) => void;
  onRetry?: (feedbackIds: string[], nodeId: string, rationale: string) => Promise<void> | void;
  onLoadAssetImpactEvents?: (reminderId: string) => Promise<void> | void;
  onResolveAssetImpactReminder?: (
    reminderId: string,
    resolutionKind: AssetImpactResolutionKind,
    note: string,
  ) => Promise<void> | void;
  onOpenNodeInGraph?: (nodeId: string) => void;
  onOpenEdgeInGraph?: (edgeId: string) => void;
}

type CategoryFilter = "ALL" | string;
type AssetKindFilter = "ALL" | string;

interface CategoryTab {
  id: CategoryFilter;
  label: string;
  visibleGroups: number;
  allItems: number;
}

const FALLBACK_CATEGORY = "review";

const FALLBACK_CATEGORY_LABELS: Record<string, string> = {
  asset_binding: "Asset binding",
  backlog: "Backlog",
  config: "Config",
  config_binding: "Config binding",
  doc: "Docs",
  doc_binding: "Doc binding",
  graph_enrich_config: "Graph enrich config",
  graph_structure: "Graph structure",
  other: "Other",
  review: "Review",
  semantic: "Semantic",
  status_observation: "Status observation",
  test: "Tests",
  test_binding: "Test binding",
};

const ASSET_IMPACT_KIND_ORDER = ["doc", "test", "config"];

const ASSET_IMPACT_KIND_LABELS: Record<string, string> = {
  config: "Config",
  doc: "Docs",
  test: "Tests",
};

const ASSET_IMPACT_LANE = "asset_impact";

// MF-2026-05-10-016 P2: per-item review surface with clickable target +
// Accept/Retry/Reject actions. Retry opens a modal for a rationale, then
// orchestrates reject_false_positive → append /semantic-feedback (JSONL) →
// re-enqueue /semantic/jobs. The next AI run sees the rationale in
// review_feedback alongside the rejected proposal in existing_semantic.
export default function ReviewQueueView({
  feedback,
  assetImpactReminders,
  assetImpactReminderEvents,
  assetImpactBusyId,
  assetImpactError,
  onDecide,
  onRetry,
  onLoadAssetImpactEvents,
  onResolveAssetImpactReminder,
  onOpenNodeInGraph,
  onOpenEdgeInGraph,
}: Props) {
  const s = feedback.summary;
  const groups = feedback.groups ?? [];
  const empty = groups.length === 0 && s.raw_count === 0;
  const assetImpactCount = assetImpactReminderList(assetImpactReminders).length;
  const [busyId, setBusyId] = useState<string | null>(null);
  const [retryGroup, setRetryGroup] = useState<FeedbackQueueGroup | null>(null);
  const [categoryFilter, setCategoryFilter] = useState<CategoryFilter>("ALL");
  const categoryTabs = useMemo(
    () => buildCategoryTabs(feedback),
    [feedback],
  );
  const filteredGroups = useMemo(
    () =>
      categoryFilter === "ALL"
        ? groups
        : groups.filter((group) => groupCategory(group) === categoryFilter),
    [categoryFilter, groups],
  );

  const dispatch = async (group: FeedbackQueueGroup, action: string) => {
    if (!onDecide) return;
    setBusyId(group.queue_id);
    try {
      await onDecide(
        group.feedback_ids,
        action,
        `${group.target_type} ${group.target_id}`,
      );
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="view">
      <div className="view-head">
        <h2 className="view-title">Review Queue</h2>
        <span className="view-subtitle">
          source <span className="mono">/feedback/queue?require_current_semantic=false</span>
        </span>
      </div>

      <div className="section">
        <div className="section-head">Summary</div>
        <div className="score-grid">
          <Card label="Raw count" v={s.raw_count} />
          <Card label="Visible groups" v={s.visible_group_count} />
          <Card label="Visible items" v={s.visible_item_count} />
          <Card label="Asset reminders" v={assetImpactCount} />
        </div>
      </div>

      <AssetImpactReminderSection
        response={assetImpactReminders}
        eventsByReminder={assetImpactReminderEvents}
        busyId={assetImpactBusyId}
        error={assetImpactError}
        onLoadEvents={onLoadAssetImpactEvents}
        onResolve={onResolveAssetImpactReminder}
        onOpenNodeInGraph={onOpenNodeInGraph}
      />

      <div className="asset-inbox-toolbar card">
        <div className="backlog-filter-group" role="tablist" aria-label="Review queue category filter">
          {categoryTabs.map((category) => (
            <button
              key={category.id}
              role="tab"
              aria-selected={categoryFilter === category.id}
              className={`chip ${categoryFilter === category.id ? "on" : "off"}`}
              onClick={() => setCategoryFilter(category.id)}
              title={
                category.id === "ALL"
                  ? "All visible review queue groups"
                  : `${category.label} · ${category.allItems} item${category.allItems === 1 ? "" : "s"}`
              }
            >
              {category.label}
              <span className="asset-chip-count">{category.visibleGroups}</span>
            </button>
          ))}
        </div>
        <span className="head-hint" style={{ marginLeft: "auto" }}>
          {filteredGroups.length} shown
        </span>
      </div>

      <div className="section">
        <div className="section-head">
          Items{" "}
          <span style={{ fontWeight: 400, color: "var(--ink-400)", fontSize: 11 }}>
            — Accept promotes semantic, Retry re-runs AI with your rationale, Reject discards
          </span>
        </div>
        {empty ? (
          <div className="empty">Review queue is empty.</div>
        ) : groups.length === 0 ? (
          <div className="empty">
            All items hidden by current filter (raw_count={s.raw_count}).
          </div>
        ) : filteredGroups.length === 0 ? (
          <div className="empty">No review items match the selected category.</div>
        ) : (
          <div className="card">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 140 }}>Category</th>
                  <th style={{ width: 110 }}>Lane</th>
                  <th style={{ width: 60 }}>Type</th>
                  <th style={{ width: 140 }}>Target</th>
                  <th>Issue</th>
                  <th style={{ width: 130 }}>Semantic gate</th>
                  <th style={{ width: 70 }}>Priority</th>
                  <th style={{ width: 260 }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredGroups.map((g) => {
                  const busy = busyId === g.queue_id;
                  const gate = g.semantic_review_gate;
                  const gateReason = gate?.reason ?? "—";
                  const gateReady = gate?.ready;
                  const isNode = g.target_type === "node";
                  const category = groupCategory(g);
                  return (
                    <tr key={g.queue_id}>
                      <td>
                        <span className={`status-badge ${categoryBadgeClass(category)}`}>
                          {categoryLabel(category, feedback.action_catalog, [g])}
                        </span>
                      </td>
                      <td>
                        <span className="mono">{g.lane}</span>
                      </td>
                      <td>
                        <span className="mono">{g.target_type}</span>
                      </td>
                      <td>
                        {isNode && onOpenNodeInGraph ? (
                          <button
                            className="target-link"
                            title={`View details · open ${g.target_id} in the graph`}
                            onClick={() => onOpenNodeInGraph(g.target_id)}
                          >
                            <span className="mono target-link-id">{g.target_id}</span>
                            <span className="target-link-arrow" aria-hidden>↗</span>
                            <span className="target-link-hint">View details</span>
                          </button>
                        ) : g.target_type === "edge" && onOpenEdgeInGraph ? (
                          <button
                            className="target-link"
                            title={`View details · pin edge ${g.target_id} in the graph`}
                            onClick={() => onOpenEdgeInGraph(g.target_id)}
                          >
                            <span className="mono target-link-id">{g.target_id}</span>
                            <span className="target-link-arrow" aria-hidden>↗</span>
                            <span className="target-link-hint">View details</span>
                          </button>
                        ) : (
                          <span className="mono">{g.target_id}</span>
                        )}
                      </td>
                      <td>
                        <div>{g.representative_issue}</div>
                        <div style={{ fontSize: 10.5, color: "var(--ink-400)", marginTop: 2 }}>
                          <span className="mono">{g.representative_feedback_id}</span>
                          {g.feedback_ids.length > 1 ? ` +${g.feedback_ids.length - 1} more` : ""}
                          {g.confidence != null ? ` · conf=${g.confidence.toFixed(2)}` : ""}
                          {g.created_at ? ` · ${shortDate(g.created_at)}` : ""}
                        </div>
                      </td>
                      <td>
                        <span
                          className="mono"
                          style={{
                            color: gateReady ? "var(--ink-700)" : "var(--ink-400)",
                            fontSize: 10.5,
                          }}
                          title={gateReady ? "ready for accept" : "underlying semantic not current"}
                        >
                          {gateReady ? "✓ " : "○ "}
                          {gateReason}
                        </span>
                      </td>
                      <td>
                        <span className="mono">{g.priority ?? "—"}</span>
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                          <button
                            className="action-btn"
                            disabled={busy || !onDecide}
                            title="POST /feedback/decision action=accept_semantic_enrichment"
                            onClick={() => dispatch(g, "accept_semantic_enrichment")}
                          >
                            {busy ? "…" : "Accept"}
                          </button>
                          <button
                            className="action-btn"
                            disabled={busy || !onRetry || !isNode}
                            title="Reject + re-enqueue with rationale (next AI run sees the prior proposal + your reason)"
                            onClick={() => setRetryGroup(g)}
                          >
                            Retry
                          </button>
                          <button
                            className="action-btn action-btn-danger"
                            disabled={busy || !onDecide}
                            title="POST /feedback/decision action=reject_false_positive"
                            onClick={() => dispatch(g, "reject_false_positive")}
                          >
                            {busy ? "…" : "Reject"}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="section">
        <div className="section-head">Categories / lanes (visible groups)</div>
        <div className="card">
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 220 }}>Category</th>
                <th>Lane</th>
                <th style={{ width: 90 }}>Visible</th>
              </tr>
            </thead>
            <tbody>
              {categoryTabs
                .filter((category) => category.id !== "ALL")
                .map((category) => (
                  <tr key={`category-${category.id}`}>
                    <td>{category.label}</td>
                    <td>
                      <span className="mono">{category.id}</span>
                    </td>
                    <td>
                      <span className="mono">{String(category.visibleGroups)}</span>
                    </td>
                  </tr>
                ))}
              {Object.entries(s.by_lane_visible_groups ?? {}).map(([lane, n]) => (
                <tr key={`lane-${lane}`}>
                  <td>
                    <span className="muted">Lane</span>
                  </td>
                  <td>{lane}</td>
                  <td>
                    <span className="mono">{String(n)}</span>
                  </td>
                </tr>
              ))}
              {categoryTabs.length <= 1 && Object.keys(s.by_lane_visible_groups ?? {}).length === 0 ? (
                <tr>
                  <td colSpan={3} className="empty" style={{ padding: 12 }}>
                    No lanes.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>

      <div className="section">
        <div className="section-head">Hidden / dropped</div>
        <div className="card card-padded">
          <div className="kv" style={{ gridTemplateColumns: "200px 1fr 200px 1fr" }}>
            <span className="k">hidden_status_observation</span>
            <span className="v">{s.hidden_status_observation_count ?? 0}</span>
            <span className="k">hidden_resolved</span>
            <span className="v">{s.hidden_resolved_count ?? 0}</span>
            <span className="k">hidden_claimed</span>
            <span className="v">{s.hidden_claimed_count ?? 0}</span>
            <span className="k">hidden_semantic_pending</span>
            <span className="v">{s.hidden_semantic_pending_count ?? 0}</span>
          </div>
        </div>
      </div>

      {retryGroup && onRetry ? (
        <RetryFeedbackModal
          targetType={retryGroup.target_type}
          targetId={retryGroup.target_id}
          feedbackIds={retryGroup.feedback_ids}
          priorIssue={retryGroup.representative_issue}
          onCancel={() => setRetryGroup(null)}
          onSubmit={async (rationale) => {
            setBusyId(retryGroup.queue_id);
            try {
              await onRetry(retryGroup.feedback_ids, retryGroup.target_id, rationale);
              setRetryGroup(null);
            } finally {
              setBusyId(null);
            }
          }}
        />
      ) : null}
    </div>
  );
}

function AssetImpactReminderSection({
  response,
  eventsByReminder = {},
  busyId,
  error,
  onLoadEvents,
  onResolve,
  onOpenNodeInGraph,
}: {
  response?: AssetImpactRemindersResponse;
  eventsByReminder?: Record<string, AssetImpactReminderEventsResponse>;
  busyId?: string | null;
  error?: string | null;
  onLoadEvents?: (reminderId: string) => Promise<void> | void;
  onResolve?: (
    reminderId: string,
    resolutionKind: AssetImpactResolutionKind,
    note: string,
  ) => Promise<void> | void;
  onOpenNodeInGraph?: (nodeId: string) => void;
}) {
  const reminders = useMemo(() => assetImpactReminderList(response), [response]);
  const [kindFilter, setKindFilter] = useState<AssetKindFilter>("ALL");
  const [expandedReminderId, setExpandedReminderId] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const kindTabs = useMemo(() => buildAssetImpactKindTabs(reminders), [reminders]);
  const filteredReminders = useMemo(
    () =>
      kindFilter === "ALL"
        ? reminders
        : reminders.filter((reminder) => assetImpactCategory(reminder) === kindFilter),
    [kindFilter, reminders],
  );
  const unavailable = Boolean(response?.unavailable);

  const setReminderNote = (reminderId: string, note: string) => {
    setNotes((current) => ({ ...current, [reminderId]: note }));
  };

  return (
    <div className="section">
      <div className="section-head">
        Asset impact reminders{" "}
        <span className="head-hint">
          lane <span className="mono">{ASSET_IMPACT_LANE}</span> · source{" "}
          <span className="mono">/asset-impact/reminders?asset_kind=&status=pending</span>
        </span>
      </div>
      <div className="asset-inbox-toolbar card">
        <div className="backlog-filter-group" role="tablist" aria-label="Asset impact reminder category filter">
          {kindTabs.map((kind) => (
            <button
              key={kind.id}
              role="tab"
              aria-selected={kindFilter === kind.id}
              className={`chip ${kindFilter === kind.id ? "on" : "off"}`}
              onClick={() => setKindFilter(kind.id)}
              title={kind.id === "ALL" ? "All pending asset impact reminders" : kind.label}
            >
              {kind.label}
              <span className="asset-chip-count">{kind.visibleGroups}</span>
            </button>
          ))}
        </div>
        <span className="head-hint" style={{ marginLeft: "auto" }}>
          {filteredReminders.length} shown
        </span>
      </div>

      {unavailable ? (
        <div className="empty">
          Asset impact reminders unavailable.
          {response?.error ? (
            <>
              <br />
              <span className="mono" style={{ color: "var(--ink-700)" }}>{response.error}</span>
            </>
          ) : null}
        </div>
      ) : reminders.length === 0 ? (
        <div className="empty">No pending asset impact reminders.</div>
      ) : filteredReminders.length === 0 ? (
        <div className="empty">No asset impact reminders match the selected category.</div>
      ) : (
        <div className="card asset-impact-table-card">
          <table className="table asset-impact-table">
            <thead>
              <tr>
                <th style={{ width: 120 }}>Category</th>
                <th style={{ width: 120 }}>Lane</th>
                <th>Asset</th>
                <th style={{ width: 150 }}>Node</th>
                <th style={{ width: 90 }}>Open</th>
                <th style={{ width: 110 }}>Latest</th>
                <th style={{ width: 120 }}>Updated</th>
                <th style={{ width: 310 }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {filteredReminders.map((reminder) => {
                const reminderId = reminder.reminder_id;
                const category = assetImpactCategory(reminder);
                const lane = assetImpactLane(reminder);
                const detail = eventsByReminder[reminderId];
                const expanded = expandedReminderId === reminderId;
                const busy = busyId === `events:${reminderId}` || busyId === `resolve:${reminderId}`;
                const eventsBusy = busyId === `events:${reminderId}`;
                const resolveBusy = busyId === `resolve:${reminderId}`;
                const note = notes[reminderId] ?? "";
                return (
                  <Fragment key={reminderId}>
                    <tr>
                      <td>
                        <span className={`status-badge ${assetImpactStatusClass(category)}`}>
                          {assetImpactCategoryLabel(reminder)}
                        </span>
                      </td>
                      <td>
                        <span className="mono">{lane}</span>
                      </td>
                      <td>
                        <div className="cell-strong mono asset-impact-asset-path">{reminder.asset_path}</div>
                        <div className="asset-impact-meta">
                          <span className="mono">{reminderId}</span>
                          {reminder.first_commit_sha ? (
                            <span>from {shortCommit(reminder.first_commit_sha)}</span>
                          ) : null}
                        </div>
                      </td>
                      <td>
                        {onOpenNodeInGraph && reminder.node_id ? (
                          <button
                            className="target-link"
                            title={`View details · open ${reminder.node_id} in the graph`}
                            onClick={() => onOpenNodeInGraph(reminder.node_id)}
                          >
                            <span className="mono target-link-id">{reminder.node_id}</span>
                            <span className="target-link-arrow" aria-hidden>↗</span>
                            <span className="target-link-hint">View details</span>
                          </button>
                        ) : (
                          <span className="mono">{reminder.node_id || "—"}</span>
                        )}
                        {reminder.node_title ? (
                          <div className="asset-impact-meta">{reminder.node_title}</div>
                        ) : null}
                      </td>
                      <td>
                        <span className="mono">{reminder.impact_count ?? reminder.open_event_ids?.length ?? 0}</span>
                      </td>
                      <td>
                        <span className="mono">{shortCommit(reminder.latest_commit_sha)}</span>
                      </td>
                      <td>{reminder.updated_at ? shortDate(reminder.updated_at) : "—"}</td>
                      <td>
                        <div className="asset-impact-actions">
                          <input
                            className="asset-impact-note"
                            value={note}
                            onChange={(event) => setReminderNote(reminderId, event.target.value)}
                            placeholder="Resolution note"
                            aria-label={`Resolution note for ${reminderId}`}
                          />
                          <button
                            className="action-btn"
                            disabled={busy || !onLoadEvents}
                            title="GET reminder events"
                            onClick={() => {
                              setExpandedReminderId(expanded ? null : reminderId);
                              if (!expanded && onLoadEvents) void onLoadEvents(reminderId);
                            }}
                          >
                            {eventsBusy ? "…" : expanded ? "Hide events" : "Events"}
                          </button>
                          <button
                            className="action-btn"
                            disabled={resolveBusy || !onResolve}
                            title="POST resolve resolution_kind=updated"
                            onClick={() => onResolve?.(reminderId, "updated", note)}
                          >
                            {resolveBusy ? "…" : "Updated"}
                          </button>
                          <button
                            className="action-btn"
                            disabled={resolveBusy || !onResolve}
                            title="POST resolve resolution_kind=keep_unchanged"
                            onClick={() => onResolve?.(reminderId, "keep_unchanged", note)}
                          >
                            Keep
                          </button>
                          <button
                            className="action-btn action-btn-danger"
                            disabled={resolveBusy || !onResolve}
                            title="POST resolve resolution_kind=waived"
                            onClick={() => onResolve?.(reminderId, "waived", note)}
                          >
                            Waive
                          </button>
                        </div>
                      </td>
                    </tr>
                    {expanded ? (
                      <tr className="asset-impact-detail-row">
                        <td colSpan={8}>
                          <AssetImpactEventsPanel detail={detail} busy={eventsBusy} error={error} />
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function AssetImpactEventsPanel({
  detail,
  busy,
  error,
}: {
  detail?: AssetImpactReminderEventsResponse;
  busy: boolean;
  error?: string | null;
}) {
  if (busy && !detail) {
    return <div className="asset-impact-detail">Loading events…</div>;
  }
  if (!detail && error) {
    return <div className="asset-impact-detail asset-impact-detail-error">{error}</div>;
  }
  const events = detail?.events ?? [];
  if (events.length === 0) {
    return <div className="asset-impact-detail">No events loaded.</div>;
  }
  return (
    <div className="asset-impact-event-list">
      {events.map((event, index) => (
        <div key={`${event.id ?? "event"}-${index}`} className="asset-impact-event">
          <span className={`status-badge ${event.event_type === "resolution_recorded" ? "status-complete" : "status-running"}`}>
            {eventLabel(event.event_type)}
          </span>
          <span className="mono">{event.id ? `#${event.id}` : "event"}</span>
          <span className="mono">{shortCommit(event.commit_sha)}</span>
          {event.actor ? <span>{event.actor}</span> : null}
          {event.created_at ? <span>{shortDate(event.created_at)}</span> : null}
          {event.covers_event_ids && event.covers_event_ids.length > 0 ? (
            <span className="mono">covers {event.covers_event_ids.join(",")}</span>
          ) : null}
          {compactEvidence(event.evidence) ? <span>{compactEvidence(event.evidence)}</span> : null}
        </div>
      ))}
    </div>
  );
}

function Card({ label, v }: { label: string; v: number }) {
  return (
    <div className="score-card">
      <div className="lbl">{label}</div>
      <div className="val">{v}</div>
    </div>
  );
}

function assetImpactReminderList(response?: AssetImpactRemindersResponse): AssetImpactReminder[] {
  return response?.reminders ?? response?.items ?? [];
}

function buildAssetImpactKindTabs(reminders: AssetImpactReminder[]): CategoryTab[] {
  const kinds = stableUnique(reminders.map(assetImpactCategory));
  const tabs = kinds
    .filter(Boolean)
    .sort((a, b) => {
      const byOrder = assetImpactKindOrder(a) - assetImpactKindOrder(b);
      if (byOrder !== 0) return byOrder;
      const byCount =
        reminders.filter((reminder) => assetImpactCategory(reminder) === b).length -
        reminders.filter((reminder) => assetImpactCategory(reminder) === a).length;
      if (byCount !== 0) return byCount;
      return a.localeCompare(b);
    })
    .map((kind) => ({
      id: kind,
      label: ASSET_IMPACT_KIND_LABELS[kind] ?? titleize(kind),
      visibleGroups: reminders.filter((reminder) => assetImpactCategory(reminder) === kind).length,
      allItems: reminders.filter((reminder) => assetImpactCategory(reminder) === kind).length,
    }));
  return [
    {
      id: "ALL",
      label: "All",
      visibleGroups: reminders.length,
      allItems: reminders.length,
    },
    ...tabs,
  ];
}

function assetImpactKindOrder(kind: string): number {
  const index = ASSET_IMPACT_KIND_ORDER.indexOf(kind);
  return index === -1 ? ASSET_IMPACT_KIND_ORDER.length : index;
}

function assetImpactCategory(reminder: AssetImpactReminder): string {
  return reminder.category?.trim() || reminder.asset_kind?.trim() || "unknown";
}

function assetImpactCategoryLabel(reminder: AssetImpactReminder): string {
  if (reminder.category_label) return reminder.category_label;
  const category = assetImpactCategory(reminder);
  return ASSET_IMPACT_KIND_LABELS[category] ?? titleize(category);
}

function assetImpactLane(reminder: AssetImpactReminder): string {
  return reminder.lane?.trim() || ASSET_IMPACT_LANE;
}

function assetImpactStatusClass(category: string): string {
  if (category === "doc") return "status-running";
  if (category === "test") return "status-complete";
  if (category === "config") return "status-pending";
  return "status-unknown";
}

function shortCommit(value?: string): string {
  const trimmed = (value ?? "").trim();
  return trimmed ? trimmed.slice(0, 7) : "—";
}

function eventLabel(value: string): string {
  if (value === "impact_detected") return "Impact";
  if (value === "resolution_recorded") return "Resolution";
  return titleize(value);
}

function compactEvidence(evidence?: Record<string, unknown>): string {
  if (!evidence) return "";
  return Object.entries(evidence)
    .filter(([key]) => key !== "schema_version")
    .slice(0, 3)
    .map(([key, value]) => `${key}=${formatEvidenceValue(value)}`)
    .join(" · ");
}

function formatEvidenceValue(value: unknown): string {
  if (Array.isArray(value)) return `[${value.length}]`;
  if (value && typeof value === "object") return "{…}";
  const text = String(value ?? "");
  return text.length > 48 ? `${text.slice(0, 45)}...` : text;
}

function buildCategoryTabs(feedback: FeedbackQueueResponse): CategoryTab[] {
  const groups = feedback.groups ?? [];
  const summary = feedback.summary;
  const visibleByCategory = summary.by_category_visible_groups;
  const allItemsByCategory = summary.by_category_all_items ?? {};
  const ids =
    visibleByCategory && Object.keys(visibleByCategory).length > 0
      ? Object.keys(visibleByCategory)
      : stableUnique(groups.map(groupCategory));

  const categoryTabs = ids
    .filter(Boolean)
    .sort((a, b) => categorySort(a, b, visibleByCategory, groups, feedback.action_catalog))
    .map((id) => {
      const matchingGroups = groups.filter((group) => groupCategory(group) === id);
      const visibleGroups = visibleByCategory?.[id] ?? matchingGroups.length;
      const allItems =
        allItemsByCategory[id] ??
        matchingGroups.reduce((total, group) => total + (group.item_count || group.feedback_ids.length || 0), 0);
      return {
        id,
        label: categoryLabel(id, feedback.action_catalog, matchingGroups),
        visibleGroups,
        allItems,
      };
    });

  return [
    {
      id: "ALL",
      label: "All",
      visibleGroups: summary.visible_group_count ?? groups.length,
      allItems: summary.visible_item_count ?? groups.reduce((total, group) => total + (group.item_count || 0), 0),
    },
    ...categoryTabs,
  ];
}

function groupCategory(group: FeedbackQueueGroup): string {
  return group.category?.trim() || FALLBACK_CATEGORY;
}

function categoryLabel(
  category: string,
  actionCatalog?: FeedbackActionCatalog,
  groups: FeedbackQueueGroup[] = [],
): string {
  const groupLabel = groups.find((group) => group.category === category && group.category_label)?.category_label;
  if (groupLabel) return groupLabel;
  const catalogLabel = actionCatalog?.category_labels?.[category];
  if (catalogLabel) return catalogLabel;
  const catalogEntry = actionCatalog?.categories?.[category];
  if (typeof catalogEntry === "string") return catalogEntry;
  if (catalogEntry?.label) return catalogEntry.label;
  return FALLBACK_CATEGORY_LABELS[category] ?? titleize(category);
}

function categorySort(
  a: string,
  b: string,
  visibleByCategory: Record<string, number> | undefined,
  groups: FeedbackQueueGroup[],
  actionCatalog?: FeedbackActionCatalog,
): number {
  const byOrder = categoryOrder(a, actionCatalog) - categoryOrder(b, actionCatalog);
  if (byOrder !== 0) return byOrder;
  const byCount =
    (visibleByCategory?.[b] ?? groups.filter((group) => groupCategory(group) === b).length) -
    (visibleByCategory?.[a] ?? groups.filter((group) => groupCategory(group) === a).length);
  if (byCount !== 0) return byCount;
  return a.localeCompare(b);
}

function categoryOrder(category: string, actionCatalog?: FeedbackActionCatalog): number {
  const order =
    actionCatalog?.category_order && actionCatalog.category_order.length > 0
      ? actionCatalog.category_order
      : [
          "semantic",
          "graph_structure",
          "graph_enrich_config",
          "asset_binding",
          "doc_binding",
          "test_binding",
          "config_binding",
          "status_observation",
          "backlog",
          "other",
          "review",
        ];
  const index = order.indexOf(category);
  return index === -1 ? order.length : index;
}

function categoryBadgeClass(category: string): string {
  if (category === "semantic") return "status-running";
  if (
    category === "asset_binding" ||
    category === "doc_binding" ||
    category === "test_binding" ||
    category === "config_binding"
  ) {
    return "status-complete";
  }
  if (category === "graph_structure" || category === "graph_enrich_config") return "status-pending";
  if (category === "status_observation") return "status-not-queued";
  return "status-unknown";
}

function stableUnique(values: string[]): string[] {
  return Array.from(new Set(values));
}

function titleize(value: string): string {
  return value
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function shortDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
  return d.toISOString().slice(5, 16).replace("T", " ");
}
