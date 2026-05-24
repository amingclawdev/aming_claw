import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../lib/api";
import type {
  BacklogBug,
  BacklogResponse,
  BacklogTimelineGateResponse,
  MfCloseTimelineGate,
  TaskTimelineEvent,
} from "../types";

interface Props {
  backlog: BacklogResponse;
  projectId: string;
}

type StatusFilter = "OPEN" | "FIXED" | "ALL";
type PriorityFilter = "ALL" | "P0" | "P1" | "P2" | "P3";

const PRIORITIES: PriorityFilter[] = ["ALL", "P0", "P1", "P2", "P3"];
const PRIORITY_WEIGHT: Record<string, number> = { P0: 0, P1: 1, P2: 2, P3: 3 };
const CLOSED_STATUSES = new Set(["FIXED", "CLOSED", "DONE", "RESOLVED", "CANCELLED"]);

interface TimelineState {
  expanded: boolean;
  loading: boolean;
  loaded: boolean;
  error: string;
  events: TaskTimelineEvent[];
  count?: number;
  gate?: BacklogTimelineGateResponse;
}

export default function BacklogView({ backlog, projectId }: Props) {
  const bugs = backlog.bugs ?? [];
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("OPEN");
  const [priorityFilter, setPriorityFilter] = useState<PriorityFilter>("ALL");
  const [query, setQuery] = useState("");
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const [timelineByBug, setTimelineByBug] = useState<Record<string, TimelineState>>({});

  const stats = useMemo(() => {
    if (backlog.summary) {
      return {
        total: backlog.summary.total,
        open: backlog.summary.open,
        fixed: backlog.summary.fixed,
        urgent: backlog.summary.urgent_open,
      };
    }
    const open = bugs.filter(isOpenBug);
    return {
      total: backlog.total_count ?? bugs.length,
      open: open.length,
      fixed: bugs.filter((b) => normalizeStatus(b.status) === "FIXED").length,
      urgent: open.filter((b) => ["P0", "P1"].includes(normalizePriority(b.priority))).length,
    };
  }, [backlog.summary, backlog.total_count, bugs]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return bugs
      .filter((bug) => {
        if (statusFilter === "OPEN" && !isOpenBug(bug)) return false;
        if (statusFilter === "FIXED" && normalizeStatus(bug.status) !== "FIXED") return false;
        if (priorityFilter !== "ALL" && normalizePriority(bug.priority) !== priorityFilter) return false;
        if (!q) return true;
        const hay = [
          bug.bug_id,
          bug.title,
          bug.details_md,
          bug.status,
          bug.priority,
          ...listFrom(bug.target_files),
          ...listFrom(bug.test_files),
          ...listFrom(bug.acceptance_criteria),
        ]
          .join(" ")
          .toLowerCase();
        return hay.includes(q);
      })
      .slice()
      .sort(compareBugs);
  }, [bugs, priorityFilter, query, statusFilter]);

  const filteredCount = backlog.filtered_count ?? stats.total;
  const pageNote = backlog.has_more ? ` · next offset ${backlog.next_offset ?? rows.length}` : "";
  const syncCommands = [
    `aming-claw backlog export --project-id ${projectId} --output backlog.json`,
    `aming-claw backlog import --project-id ${projectId} --input backlog.json --dry-run`,
    `aming-claw backlog import --project-id ${projectId} --input backlog.json`,
  ].join("\n");

  useEffect(() => {
    setTimelineByBug({});
  }, [projectId]);

  const copySyncCommands = async () => {
    try {
      await navigator.clipboard.writeText(syncCommands);
      setCopyState("copied");
      window.setTimeout(() => setCopyState("idle"), 1800);
    } catch {
      setCopyState("failed");
      window.setTimeout(() => setCopyState("idle"), 2400);
    }
  };

  const toggleTimeline = (bugId: string) => {
    const current = timelineByBug[bugId];
    if (current?.expanded) {
      setTimelineByBug((states) => ({
        ...states,
        [bugId]: { ...states[bugId], expanded: false },
      }));
      return;
    }

    setTimelineByBug((states) => {
      const existing = states[bugId];
      return {
        ...states,
        [bugId]: {
          expanded: true,
          loading: existing?.loaded ? false : true,
          loaded: existing?.loaded ?? false,
          error: "",
          events: existing?.events ?? [],
          count: existing?.count,
          gate: existing?.gate,
        },
      };
    });

    if (current?.loaded || current?.loading) return;

    api.backlogTimelineGateFor(projectId, bugId, 50)
      .then((res) => {
        setTimelineByBug((states) => {
          const existing = states[bugId];
          return {
            ...states,
            [bugId]: {
              expanded: existing?.expanded ?? true,
              loading: false,
              loaded: true,
              error: "",
              events: res.events ?? [],
              count: res.event_count ?? res.events?.length ?? 0,
              gate: res,
            },
          };
        });
      })
      .catch((error) => {
        const msg = error instanceof ApiError ? `${error.message} ${error.body}` : String(error);
        setTimelineByBug((states) => {
          const existing = states[bugId];
          return {
            ...states,
            [bugId]: {
              expanded: existing?.expanded ?? true,
              loading: false,
              loaded: true,
              error: msg,
              events: existing?.events ?? [],
              count: existing?.count,
              gate: existing?.gate,
            },
          };
        });
      });
  };

  return (
    <div className="view">
      <div className="view-head">
        <h2 className="view-title">Backlog</h2>
        <span className="view-subtitle">
          source <span className="mono">/api/backlog/{projectId}</span> ·{" "}
          {rows.length} shown · {filteredCount} filtered · {stats.total} total{pageNote}
        </span>
      </div>

      <div className="backlog-guidance">
        <div>
          <strong>Project memory.</strong> Backlog rows live in the local governance DB. Git/plugin updates move code;
          they do not sync backlog rows unless you import a portable export.
        </div>
        <button className="action-btn" onClick={copySyncCommands} title="Copy portable backlog export/import commands">
          {copyState === "copied" ? "Copied sync commands" : copyState === "failed" ? "Copy failed" : "Copy sync commands"}
        </button>
      </div>

      <div className="score-grid backlog-score-grid">
        <Kpi label="Open" value={stats.open} tone={stats.open > 0 ? "amber" : "green"} />
        <Kpi label="P0/P1 open" value={stats.urgent} tone={stats.urgent > 0 ? "red" : "neutral"} />
        <Kpi label="Fixed" value={stats.fixed} tone="green" />
        <Kpi label="Total" value={stats.total} tone="blue" />
      </div>

      <div className="backlog-toolbar card">
        <div className="backlog-filter-group">
          {(["OPEN", "FIXED", "ALL"] as StatusFilter[]).map((s) => (
            <button
              key={s}
              className={`chip ${statusFilter === s ? "on" : "off"}`}
              onClick={() => setStatusFilter(s)}
            >
              {s === "ALL" ? "All status" : s}
            </button>
          ))}
        </div>
        <div className="backlog-filter-group">
          {PRIORITIES.map((p) => (
            <button
              key={p}
              className={`chip ${priorityFilter === p ? "on" : "off"}`}
              onClick={() => setPriorityFilter(p)}
            >
              {p === "ALL" ? "All priority" : p}
            </button>
          ))}
        </div>
        <input
          className="backlog-search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search backlog, files, criteria..."
        />
      </div>

      <div className="section">
        <div className="section-head">
          Rows <span className="head-hint">read-only, sorted by priority and updated time</span>
        </div>
        {rows.length === 0 ? (
          <div className="empty">
            No backlog rows match the current filters.
            <div className="empty-hint">
              Use an AI-backed graph action to file a row with node/file context.
            </div>
          </div>
        ) : (
          <div className="card">
            <table className="table backlog-table">
              <thead>
                <tr>
                  <th style={{ width: 82 }}>Priority</th>
                  <th style={{ width: 94 }}>Status</th>
                  <th>Backlog</th>
                  <th style={{ width: 260 }}>Scope</th>
                  <th style={{ width: 132 }}>Runtime</th>
                  <th style={{ width: 112 }}>Updated</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((bug) => (
                  <BacklogRow
                    key={bug.bug_id}
                    bug={bug}
                    timeline={timelineByBug[bug.bug_id]}
                    onToggleTimeline={() => toggleTimeline(bug.bug_id)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function BacklogRow({
  bug,
  timeline,
  onToggleTimeline,
}: {
  bug: BacklogBug;
  timeline?: TimelineState;
  onToggleTimeline: () => void;
}) {
  const files = listFrom(bug.target_files);
  const criteria = listFrom(bug.acceptance_criteria);
  const runtime = bug.runtime_state || bug.chain_stage || bug.mf_type || "idle";
  const contract = bug.contract_summary;
  return (
    <>
      <tr>
        <td>
          <span className={`backlog-priority tone-${priorityTone(bug.priority)}`}>
            {normalizePriority(bug.priority)}
          </span>
        </td>
        <td>
          <span className={`status-badge ${statusClass(bug.status)}`}>
            {normalizeStatus(bug.status)}
          </span>
        </td>
        <td className="backlog-title-cell">
          <div className="cell-strong">{bug.title || bug.bug_id}</div>
          <div className="cell-mono-id">{bug.bug_id}</div>
          <button
            type="button"
            className={`timeline-toggle ${timeline?.expanded ? "on" : "off"}`}
            onClick={onToggleTimeline}
            aria-expanded={timeline?.expanded ?? false}
          >
            {timeline?.expanded ? "Hide evidence" : "Show evidence"}
            {timeline?.loaded && !timeline.error ? (
              <span className="timeline-toggle-count">{timeline.count ?? timeline.events.length}</span>
            ) : null}
          </button>
          {bug.details_md ? <div className="backlog-details">{truncate(bug.details_md, 220)}</div> : null}
          {criteria.length > 0 ? (
            <div className="backlog-criteria">
              {criteria.slice(0, 2).map((item) => (
                <span key={item}>{item}</span>
              ))}
              {criteria.length > 2 ? <em>+{criteria.length - 2}</em> : null}
            </div>
          ) : null}
        </td>
        <td>
          {files.length > 0 ? (
            <div className="backlog-file-list">
              {files.slice(0, 4).map((file) => (
                <span className="mono" key={file} title={file}>
                  {file}
                </span>
              ))}
              {files.length > 4 ? <em>+{files.length - 4} more</em> : null}
            </div>
          ) : (
            <span className="muted">No target files</span>
          )}
        </td>
        <td>
          <div className="mono">{runtime}</div>
          {contract?.has_contract ? (
            <div className="backlog-commit mono" title="Contract evidence requirements">
              contract {contract.template_id || contract.contract_instance_id || "declared"} · req {contract.required_evidence_count ?? 0}
            </div>
          ) : null}
          {bug.commit ? <div className="backlog-commit mono">{shortCommit(bug.commit)}</div> : null}
          {bug.worktree_branch ? <div className="backlog-commit mono">{bug.worktree_branch}</div> : null}
        </td>
        <td>
          <span className="mono">{shortDate(bug.updated_at || bug.created_at || bug.fixed_at)}</span>
        </td>
      </tr>
      {timeline?.expanded ? (
        <tr className="backlog-timeline-row">
          <td colSpan={6}>
            <TimelinePanel timeline={timeline} backlogId={bug.bug_id} />
          </td>
        </tr>
      ) : null}
    </>
  );
}

function TimelinePanel({ timeline, backlogId }: { timeline: TimelineState; backlogId: string }) {
  const count = timeline.count ?? timeline.events.length;
  const gate = timeline.gate?.timeline_gate;
  const lanes = buildTimelineLanes(timeline.events);
  return (
    <div className="backlog-timeline-panel">
      <div className="backlog-timeline-head">
        <span>Execution evidence</span>
        <span className="mono">
          {backlogId} · {count} event{count === 1 ? "" : "s"}
        </span>
      </div>
      {timeline.loading ? <div className="timeline-empty">Loading timeline...</div> : null}
      {timeline.error ? <div className="timeline-empty timeline-error">Timeline load failed: {timeline.error}</div> : null}
      {!timeline.loading && !timeline.error && timeline.events.length === 0 ? (
        <div className="timeline-empty">No execution events linked to this backlog row.</div>
      ) : null}
      {!timeline.loading && !timeline.error && gate ? (
        <GateSummary gate={gate} response={timeline.gate} />
      ) : null}
      {!timeline.loading && !timeline.error && lanes.length > 0 ? (
        <div className="backlog-lane-grid" aria-label="One-hop agent lanes">
          {lanes.map((lane) => (
            <div className="backlog-lane-card" key={lane.id}>
              <div className="backlog-lane-head">
                <span>{lane.label}</span>
                <span className="mono">{lane.events.length} event{lane.events.length === 1 ? "" : "s"}</span>
              </div>
              <div className="backlog-lane-meta">
                <span className={`status-badge ${statusClass(lane.latestStatus)}`}>
                  {lane.latestStatus || "unknown"}
                </span>
                {lane.latestActor ? <span>{lane.latestActor}</span> : null}
                {lane.latestCommit ? <span className="mono">{shortCommit(lane.latestCommit)}</span> : null}
              </div>
              {lane.blockers.length > 0 ? (
                <div className="backlog-lane-blockers">
                  {lane.blockers.slice(0, 2).map((blocker) => (
                    <span key={blocker}>{blocker}</span>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      {!timeline.loading && !timeline.error && timeline.events.length > 0 ? (
        <div className="backlog-timeline-list">
          {timeline.events.map((event, index) => (
            <div className="backlog-timeline-event" key={timelineEventKey(event, index)}>
              <div className="backlog-timeline-meta">
                <span className={`status-badge ${statusClass(event.status || event.event_type)}`}>
                  {event.status || event.event_type || "event"}
                </span>
                <span className="mono">{event.event_type || "unknown_event"}</span>
                {event.event_kind ? <span className="mono">{event.event_kind}</span> : null}
                <span className="mono">{shortDateTime(event.created_at)}</span>
                <span>{event.actor || "actor unknown"}</span>
                <span className="mono">event {timelineEventId(event)}</span>
              </div>
              <div className="backlog-timeline-facts">
                <span className="mono">lane {laneLabelForEvent(event)}</span>
                <span>{formatVerification(event.verification)}</span>
                <span>{formatArtifactRefs(event.artifact_refs)}</span>
                {event.task_id ? <span className="mono">task {event.task_id}</span> : null}
                {event.attempt_num ? <span className="mono">attempt {event.attempt_num}</span> : null}
                {event.commit_sha ? <span className="mono">commit {shortCommit(event.commit_sha)}</span> : null}
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function GateSummary({
  gate,
  response,
}: {
  gate: MfCloseTimelineGate;
  response?: BacklogTimelineGateResponse;
}) {
  const contract = gate.contract_gate;
  const present = gate.present_event_kinds ?? [];
  const missing = gate.missing_event_kinds ?? [];
  const required = gate.required_event_kinds ?? [];
  const contractRequired = contract?.required_requirement_ids ?? [];
  const contractPresent = contract?.present_requirement_ids ?? [];
  const contractMissing = contract?.missing_requirement_ids ?? [];
  const contractLabel = contract?.template_id || contract?.contract_instance_id || "no contract";
  const hasContract = contractRequired.length > 0 || Boolean(contract?.template_id || contract?.contract_instance_id);
  return (
    <div className="backlog-gate-grid">
      <div className={`backlog-gate-card ${gate.passed ? "pass" : "fail"}`}>
        <div className="backlog-gate-title">
          <span>Close gate</span>
          <span className={`status-badge ${gate.passed ? "status-complete" : "status-failed"}`}>
            {response?.applicable === false ? "not applicable" : gate.status || (gate.passed ? "passed" : "blocked")}
          </span>
        </div>
        <div className="backlog-gate-facts">
          <span>required {required.length}</span>
          <span>present {present.length}</span>
          <span>missing {missing.length}</span>
          {response?.reason ? <span>{response.reason}</span> : null}
        </div>
        <TokenList label="missing" values={missing} empty="none" tone={missing.length ? "red" : "green"} />
      </div>
      <div className={`backlog-gate-card ${hasContract && contract?.passed ? "pass" : hasContract ? "fail" : "neutral"}`}>
        <div className="backlog-gate-title">
          <span>Contract</span>
          <span className={`status-badge ${hasContract && contract?.passed ? "status-complete" : hasContract ? "status-failed" : "status-unknown"}`}>
            {hasContract ? contract?.status || "blocked" : "not declared"}
          </span>
        </div>
        <div className="backlog-gate-facts">
          <span className="mono">{contractLabel}</span>
          {contract?.contract_instance_id ? <span className="mono">{contract.contract_instance_id}</span> : null}
        </div>
        <TokenList label="required" values={contractRequired} empty="none" />
        <TokenList label="present" values={contractPresent} empty="none" tone="green" />
        <TokenList label="missing" values={contractMissing} empty="none" tone={contractMissing.length ? "red" : "green"} />
      </div>
    </div>
  );
}

function TokenList({
  label,
  values,
  empty,
  tone = "neutral",
}: {
  label: string;
  values: string[];
  empty: string;
  tone?: "neutral" | "green" | "red";
}) {
  return (
    <div className={`backlog-token-list tone-${tone}`}>
      <span>{label}</span>
      {(values.length > 0 ? values : [empty]).slice(0, 8).map((value) => (
        <em key={value} className="mono">{value}</em>
      ))}
      {values.length > 8 ? <strong>+{values.length - 8}</strong> : null}
    </div>
  );
}

interface TimelineLane {
  id: string;
  label: string;
  events: TaskTimelineEvent[];
  latestStatus: string;
  latestActor: string;
  latestCommit: string;
  blockers: string[];
}

function buildTimelineLanes(events: TaskTimelineEvent[]): TimelineLane[] {
  const grouped = new Map<string, TaskTimelineEvent[]>();
  for (const event of events) {
    const lane = laneLabelForEvent(event);
    grouped.set(lane, [...(grouped.get(lane) ?? []), event]);
  }
  const preferred = ["observer", "backend", "frontend", "gate", "merge", "verification"];
  return Array.from(grouped.entries())
    .map(([id, laneEvents]) => {
      const latest = laneEvents[laneEvents.length - 1] ?? {};
      return {
        id,
        label: titleizeLane(id),
        events: laneEvents,
        latestStatus: String(latest.status || latest.decision || latest.event_kind || "unknown"),
        latestActor: latest.actor || "",
        latestCommit: latest.commit_sha || "",
        blockers: laneEvents.flatMap((event) => eventBlockers(event)).filter(Boolean),
      };
    })
    .sort((a, b) => {
      const ai = preferred.indexOf(a.id);
      const bi = preferred.indexOf(b.id);
      if (ai !== bi) return (ai < 0 ? preferred.length : ai) - (bi < 0 ? preferred.length : bi);
      return a.id.localeCompare(b.id);
    });
}

function laneLabelForEvent(event: TaskTimelineEvent): string {
  const payload = asRecord(event.payload);
  const verification = asRecord(event.verification);
  const raw =
    stringField(payload, "lane") ||
    stringField(payload, "agent_lane") ||
    stringField(payload, "worker_lane") ||
    stringField(payload, "agent_id") ||
    stringField(payload, "parallel_agent_id") ||
    stringField(verification, "lane") ||
    event.actor ||
    event.phase ||
    event.event_kind ||
    event.event_type;
  const normalized = raw.toLowerCase();
  if (normalized.includes("front")) return "frontend";
  if (normalized.includes("back")) return "backend";
  if (normalized.includes("observer")) return "observer";
  if (normalized.includes("gate") || normalized.includes("close_ready")) return "gate";
  if (normalized.includes("merge")) return "merge";
  if (normalized.includes("verify") || normalized.includes("test")) return "verification";
  if (normalized.includes("implement")) return "implementation";
  return normalized.replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "event";
}

function eventBlockers(event: TaskTimelineEvent): string[] {
  const payload = asRecord(event.payload);
  const verification = asRecord(event.verification);
  return [
    ...listUnknown(payload.blockers).map(compactUnknown),
    ...listUnknown(verification.blockers).map(compactUnknown),
    ...listUnknown(verification.errors).map(compactUnknown),
  ].filter(Boolean);
}

function stringField(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === "string" ? value.trim() : "";
}

function titleizeLane(value: string): string {
  if (value === "frontend") return "Frontend";
  if (value === "backend") return "Backend";
  if (value === "observer") return "Observer";
  if (value === "gate") return "Gate";
  if (value === "merge") return "Merge";
  if (value === "verification") return "Verification";
  return value.replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function timelineEventKey(event: TaskTimelineEvent, index: number): string {
  return String(event.event_id || event.id || event.trace_id || `${event.event_type}-${index}`);
}

function timelineEventId(event: TaskTimelineEvent): string {
  if (event.event_id) return event.event_id;
  if (event.id != null) return `#${event.id}`;
  return event.trace_id || "n/a";
}

function formatVerification(value?: TaskTimelineEvent["verification"]): string {
  const record = asRecord(value);
  const keys = Object.keys(record);
  if (keys.length === 0) return "verification: none";

  const parts: string[] = [];
  if (typeof record.passed === "boolean") parts.push(record.passed ? "passed" : "failed");
  if (typeof record.status === "string" && record.status) parts.push(record.status);
  const warnings = listUnknown(record.warnings);
  const errors = listUnknown(record.errors);
  if (errors.length > 0) parts.push(`${errors.length} error${errors.length === 1 ? "" : "s"}`);
  if (warnings.length > 0) parts.push(`${warnings.length} warning${warnings.length === 1 ? "" : "s"}`);

  const checks = asRecord(record.checks);
  const checkValues = Object.values(checks).filter((item): item is boolean => typeof item === "boolean");
  if (checkValues.length > 0) {
    const passed = checkValues.filter(Boolean).length;
    parts.push(`${passed}/${checkValues.length} checks`);
  }

  return `verification: ${parts.length > 0 ? stableUnique(parts).join(" | ") : keys.slice(0, 3).join(", ")}`;
}

function formatArtifactRefs(value?: Record<string, unknown>): string {
  const record = asRecord(value);
  const entries = Object.entries(record);
  if (entries.length === 0) return "artifacts: none";
  const summary = entries
    .slice(0, 3)
    .map(([key, item]) => `${key}: ${compactUnknown(item)}`)
    .join(" | ");
  return `artifacts: ${summary}${entries.length > 3 ? ` | +${entries.length - 3}` : ""}`;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function listUnknown(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (value == null || value === "") return [];
  return [value];
}

function compactUnknown(value: unknown): string {
  if (value == null || value === "") return "empty";
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    return value.slice(0, 2).map(compactUnknown).join(", ");
  }
  if (typeof value === "object") {
    const keys = Object.keys(value as Record<string, unknown>);
    return keys.length === 0 ? "{}" : keys.slice(0, 3).join(", ");
  }
  return String(value);
}

function stableUnique(values: string[]): string[] {
  return values.filter((value, index) => values.indexOf(value) === index);
}

function shortDateTime(value?: string): string {
  if (!value) return "-";
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return value.slice(0, 16) || "-";
  return `${new Date(time).toISOString().replace("T", " ").slice(0, 16)}Z`;
}

function Kpi({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "green" | "amber" | "red" | "blue" | "neutral";
}) {
  return (
    <div className={`score-card count-card tone-${tone}`}>
      <div className="accent-bar" />
      <div className="lbl">{label}</div>
      <div className="val">{value}</div>
    </div>
  );
}

function compareBugs(a: BacklogBug, b: BacklogBug): number {
  const openDelta = Number(isOpenBug(b)) - Number(isOpenBug(a));
  if (openDelta !== 0) return openDelta;
  const priorityDelta =
    (PRIORITY_WEIGHT[normalizePriority(a.priority)] ?? 99) -
    (PRIORITY_WEIGHT[normalizePriority(b.priority)] ?? 99);
  if (priorityDelta !== 0) return priorityDelta;
  return dateValue(b.updated_at || b.created_at || b.fixed_at) - dateValue(a.updated_at || a.created_at || a.fixed_at);
}

function isOpenBug(bug: BacklogBug): boolean {
  return !CLOSED_STATUSES.has(normalizeStatus(bug.status));
}

function normalizeStatus(status?: string): string {
  return (status || "OPEN").toUpperCase();
}

function normalizePriority(priority?: string): string {
  return (priority || "P3").toUpperCase();
}

function priorityTone(priority?: string): string {
  const p = normalizePriority(priority);
  if (p === "P0") return "red";
  if (p === "P1") return "amber";
  if (p === "P2") return "blue";
  return "neutral";
}

function statusClass(status?: string): string {
  const s = normalizeStatus(status);
  if (s === "FIXED" || s === "DONE" || s === "RESOLVED") return "status-complete";
  if (s === "FAILED" || s === "BLOCKED") return "status-failed";
  if (s === "RUNNING" || s === "CLAIMED" || s === "IN_CHAIN") return "status-running";
  if (s === "OPEN" || s === "QUEUED") return "status-pending";
  return "status-unknown";
}

function listFrom(value?: string[] | string): string[] {
  if (!value) return [];
  if (Array.isArray(value)) return value.map(String).filter(Boolean);
  const text = String(value).trim();
  if (!text) return [];
  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) return parsed.map(String).filter(Boolean);
  } catch {
    // Fall back to line/comma splitting for legacy rows.
  }
  return text
    .split(/\r?\n|,\s+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function truncate(text: string, max: number): string {
  const oneLine = text.replace(/\s+/g, " ").trim();
  if (oneLine.length <= max) return oneLine;
  return `${oneLine.slice(0, max - 1)}…`;
}

function shortDate(value?: string): string {
  if (!value) return "—";
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return value.slice(0, 10) || "—";
  return new Date(time).toISOString().slice(0, 10);
}

function dateValue(value?: string): number {
  if (!value) return 0;
  const time = Date.parse(value);
  return Number.isFinite(time) ? time : 0;
}

function shortCommit(commit: string): string {
  return commit.length > 10 ? commit.slice(0, 7) : commit;
}
