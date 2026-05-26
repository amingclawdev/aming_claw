import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
type DetailTab = "timeline" | "contract";
type ContractEvidenceStatus = "passed" | "missing" | "failed" | "bypassed" | "inferred" | "not_applicable";

const PRIORITIES: PriorityFilter[] = ["ALL", "P0", "P1", "P2", "P3"];
const PRIORITY_WEIGHT: Record<string, number> = { P0: 0, P1: 1, P2: 2, P3: 3 };
const CLOSED_STATUSES = new Set(["FIXED", "CLOSED", "DONE", "RESOLVED", "CANCELLED"]);
const BACKLOG_URL_PARAM = "backlog";
const BACKLOG_DETAIL_TIMELINE_LIMIT = 250;

export const BACKLOG_PARALLEL_TIMELINE_FIXTURE_EVENTS: TaskTimelineEvent[] = [
  {
    event_id: "fixture-observer-dispatch",
    event_type: "mf_dispatch",
    event_kind: "implementation",
    actor: "observer",
    phase: "dispatch",
    status: "accepted",
    payload: { lane: "observer", requirement_ids: ["impact_scope_analysis"], orchestration: true },
    created_at: "2026-05-25T12:00:00Z",
  },
  {
    event_id: "fixture-worker-frontend",
    event_type: "subagent_result",
    event_kind: "implementation",
    actor: "mf_sub_frontend",
    phase: "implementation",
    status: "passed",
    payload: {
      lane: "frontend",
      requirement_ids: ["parallel_timeline_dag", "evidence_inspector"],
      graph_query_trace_ids: ["gqt-fixture-frontend"],
      inspected_node_ids: ["L7.228"],
      inspected_node_titles: ["frontend.dashboard.src.views.BacklogView"],
      changed_files: ["frontend/dashboard/src/views/BacklogView.tsx", "frontend/dashboard/src/styles.css"],
      tests_written: ["frontend/dashboard/scripts/e2e-projects.mjs"],
    },
    verification: { tests_run: ["npm run build"], passed: true },
    created_at: "2026-05-25T12:01:00Z",
  },
  {
    event_id: "fixture-worker-backend",
    event_type: "subagent_result",
    event_kind: "implementation",
    actor: "mf_sub_backend",
    phase: "implementation",
    status: "passed",
    payload: {
      lane: "backend",
      requirement_ids: ["modal_summary_contract"],
      changed_files: ["agent/governance/server.py", "agent/governance/task_timeline.py"],
      tests_run: ["python -m pytest agent/tests/test_task_timeline.py -q"],
    },
    created_at: "2026-05-25T12:01:00Z",
  },
  {
    event_id: "fixture-gate-merge",
    event_type: "merge_gate",
    event_kind: "verification",
    actor: "observer",
    phase: "merge_gate",
    status: "passed",
    payload: {
      lane: "gate",
      requirement_ids: ["fixture_parallel_timeline", "no_false_evidence_gate"],
      precheck_results: { no_false_evidence_gate: true },
    },
    created_at: "2026-05-25T12:03:00Z",
  },
];

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
  const [selectedBugId, setSelectedBugId] = useState(() => readBacklogIdFromUrl());
  const [detailByBug, setDetailByBug] = useState<Record<string, BacklogBug>>({});
  const [detailLoadingByBug, setDetailLoadingByBug] = useState<Record<string, boolean>>({});
  const [detailErrorByBug, setDetailErrorByBug] = useState<Record<string, string>>({});
  const [modalTrail, setModalTrail] = useState<string[]>([]);
  const timelineByBugRef = useRef<Record<string, TimelineState>>({});

  useEffect(() => {
    timelineByBugRef.current = timelineByBug;
  }, [timelineByBug]);

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
    setDetailByBug({});
    setDetailLoadingByBug({});
    setDetailErrorByBug({});
    setModalTrail([]);
  }, [projectId]);

  useEffect(() => {
    const handlePopState = () => {
      setSelectedBugId(readBacklogIdFromUrl());
      setModalTrail([]);
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

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

  const loadTimeline = useCallback((bugId: string, expanded: boolean) => {
    const existingSnapshot = timelineByBugRef.current[bugId];
    const shouldFetch = !(existingSnapshot?.loaded || existingSnapshot?.loading);
    setTimelineByBug((states) => {
      const existing = states[bugId];
      return {
        ...states,
        [bugId]: {
          expanded,
          loading: existing?.loaded ? false : shouldFetch,
          loaded: existing?.loaded ?? false,
          error: "",
          events: existing?.events ?? [],
          count: existing?.count,
          gate: existing?.gate,
        },
      };
    });

    if (!shouldFetch) return;

    api.backlogTimelineGateFor(projectId, bugId, BACKLOG_DETAIL_TIMELINE_LIMIT)
      .then((res) => {
        setTimelineByBug((states) => {
          const existing = states[bugId];
          return {
            ...states,
            [bugId]: {
              expanded: existing?.expanded ?? expanded,
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
              expanded: existing?.expanded ?? expanded,
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
  }, [projectId]);

  const toggleTimeline = (bugId: string) => {
    const current = timelineByBug[bugId];
    if (current?.expanded) {
      setTimelineByBug((states) => ({
        ...states,
        [bugId]: { ...states[bugId], expanded: false },
      }));
      return;
    }
    loadTimeline(bugId, true);
  };

  const fetchBugDetail = useCallback((bugId: string) => {
    if (detailByBug[bugId] || detailLoadingByBug[bugId]) return;
    setDetailLoadingByBug((states) => ({ ...states, [bugId]: true }));
    setDetailErrorByBug((states) => ({ ...states, [bugId]: "" }));
    api.backlogBugFor(projectId, bugId)
      .then((bug) => {
        setDetailByBug((states) => ({ ...states, [bugId]: bug }));
      })
      .catch((error) => {
        const msg = error instanceof ApiError ? `${error.message} ${error.body}` : String(error);
        setDetailErrorByBug((states) => ({ ...states, [bugId]: msg }));
      })
      .finally(() => {
        setDetailLoadingByBug((states) => ({ ...states, [bugId]: false }));
      });
  }, [detailByBug, detailLoadingByBug, projectId]);

  const openDetail = useCallback((bugId: string, mode: "push" | "replace" = "push", keepTrail = false) => {
    setModalTrail((trail) => {
      if (!keepTrail || !selectedBugId || selectedBugId === bugId) return trail;
      return [...trail.filter((id) => id !== bugId), selectedBugId].slice(-5);
    });
    setSelectedBugId(bugId);
    writeBacklogIdToUrl(bugId, mode);
    fetchBugDetail(bugId);
    loadTimeline(bugId, false);
  }, [fetchBugDetail, loadTimeline, selectedBugId]);

  const closeDetail = useCallback(() => {
    setSelectedBugId(null);
    setModalTrail([]);
    writeBacklogIdToUrl(null, "push");
  }, []);

  const stepBackDetail = useCallback(() => {
    setModalTrail((trail) => {
      const nextTrail = trail.slice(0, -1);
      const nextId = trail[trail.length - 1];
      if (nextId) {
        setSelectedBugId(nextId);
        writeBacklogIdToUrl(nextId, "push");
        fetchBugDetail(nextId);
        loadTimeline(nextId, false);
      }
      return nextTrail;
    });
  }, [fetchBugDetail, loadTimeline]);

  useEffect(() => {
    if (!selectedBugId) return;
    fetchBugDetail(selectedBugId);
    loadTimeline(selectedBugId, false);
  }, [fetchBugDetail, loadTimeline, selectedBugId]);

  const selectedBug = selectedBugId
    ? detailByBug[selectedBugId] ?? bugs.find((bug) => bug.bug_id === selectedBugId) ?? null
    : null;

  const selectedTimeline = selectedBugId ? timelineByBug[selectedBugId] : undefined;

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
                    onOpenDetail={() => openDetail(bug.bug_id)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {selectedBugId ? (
        <BacklogDetailModal
          bug={selectedBug}
          fallbackBugId={selectedBugId}
          timeline={selectedTimeline}
          loadingBug={detailLoadingByBug[selectedBugId] ?? false}
          error={detailErrorByBug[selectedBugId] ?? ""}
          breadcrumb={modalTrail}
          onBack={stepBackDetail}
          onClose={closeDetail}
          onSelectRelated={(bugId) => openDetail(bugId, "push", true)}
        />
      ) : null}
    </div>
  );
}

function BacklogRow({
  bug,
  timeline,
  onToggleTimeline,
  onOpenDetail,
}: {
  bug: BacklogBug;
  timeline?: TimelineState;
  onToggleTimeline: () => void;
  onOpenDetail: () => void;
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
            className="backlog-detail-link"
            onClick={onOpenDetail}
            aria-label={`Open detail for ${bug.bug_id}`}
          >
            Open detail
          </button>
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
  const implementationSteps = buildImplementationSteps(timeline.events);
  const hasImplementationEvidence = implementationSteps.length > 0;
  const observerEvents = timeline.events.filter(isObserverOrchestrationEvent);
  const gateState = gateEvidenceState(timeline.gate, timeline.events);
  return (
    <div className={`backlog-timeline-panel ${hasImplementationEvidence ? "implementation-focused" : ""}`}>
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
      {!timeline.loading && !timeline.error && gateState.noGate ? (
        <NoGateNotice reason={gateState.reason} />
      ) : null}
      {!timeline.loading && !timeline.error && gate && !gateState.noGate ? (
        <GateSummary gate={gate} response={timeline.gate} />
      ) : null}
      {!timeline.loading && !timeline.error && implementationSteps.length > 0 ? (
        <ImplementationStepGrid steps={implementationSteps} observerEventCount={observerEvents.length} />
      ) : null}
      {!timeline.loading && !timeline.error && lanes.length > 0 ? (
        <div className="backlog-lane-grid" aria-label="One-hop agent lanes">
          {lanes.map((lane) => (
            <div className={`backlog-lane-card lane-${cssToken(lane.id)} ${lane.deemphasized ? "deemphasized" : ""}`} key={lane.id}>
              <div className="backlog-lane-head">
                <span>{lane.label}</span>
                <span className="mono">{lane.events.length} event{lane.events.length === 1 ? "" : "s"}</span>
              </div>
              <div className="backlog-lane-meta">
                <span className={`status-badge ${statusClass(lane.latestStatus)}`}>
                  {lane.latestStatus || "unknown"}
                </span>
                {lane.latestActor ? <span title={lane.latestActorRaw || lane.latestActor}>{lane.latestActor}</span> : null}
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
            <div
              className={`backlog-timeline-event ${isImplementationEvidenceEvent(event) ? "implementation" : ""} ${hasImplementationEvidence && isObserverOrchestrationEvent(event) ? "deemphasized" : ""}`}
              key={timelineEventKey(event, index)}
            >
              <div className="backlog-timeline-meta">
                <span className={`status-badge ${statusClass(event.status || event.event_type)}`}>
                  {event.status || event.event_type || "event"}
                </span>
                <span className="mono">{event.event_type || "unknown_event"}</span>
                {event.event_kind ? <span className="mono">{event.event_kind}</span> : null}
                <span className="mono">{shortDateTime(event.created_at)}</span>
                <span title={event.actor || ""}>{displayActorForEvent(event)}</span>
                <span className="mono">event {timelineEventId(event)}</span>
              </div>
              <div className="backlog-timeline-facts">
                <span className="mono" title={rawLaneKeyForEvent(event)}>lane {titleizeLane(laneLabelForEvent(event))}</span>
                <span>{formatVerification(event.verification)}</span>
                <span>{formatArtifactRefs(event.artifact_refs)}</span>
                {event.task_id ? <span className="mono">task {event.task_id}</span> : null}
                {event.attempt_num ? <span className="mono">attempt {event.attempt_num}</span> : null}
                {event.commit_sha ? <span className="mono">commit {shortCommit(event.commit_sha)}</span> : null}
              </div>
              <ArtifactPills summary={timelineEventArtifacts(event)} compact />
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function BacklogDetailModal({
  bug,
  fallbackBugId,
  timeline,
  loadingBug,
  error,
  breadcrumb,
  onBack,
  onClose,
  onSelectRelated,
}: {
  bug: BacklogBug | null;
  fallbackBugId: string;
  timeline?: TimelineState;
  loadingBug: boolean;
  error: string;
  breadcrumb: string[];
  onBack: () => void;
  onClose: () => void;
  onSelectRelated: (bugId: string) => void;
}) {
  const events = timeline?.events ?? [];
  const gate = timeline?.gate?.timeline_gate;
  const dag = useMemo(() => buildTimelineDag(bug, events, gate), [bug, events, gate]);
  const contractAudit = useMemo(() => buildContractAudit(bug, events, gate, timeline?.gate), [bug, events, gate, timeline?.gate]);
  const [activeTab, setActiveTab] = useState<DetailTab>("timeline");
  const [selectedNodeId, setSelectedNodeId] = useState<string>("");
  const selectedNode = dag.nodes.find((node) => node.id === selectedNodeId) ?? dag.nodes[0] ?? null;
  const title = bug?.title || fallbackBugId;

  useEffect(() => {
    if (!selectedNode || dag.nodes.some((node) => node.id === selectedNodeId)) return;
    setSelectedNodeId(selectedNode.id);
  }, [dag.nodes, selectedNode, selectedNodeId]);

  return (
    <div className="backlog-modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="backlog-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="backlog-modal-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="backlog-modal-head">
          <div>
            <div className="backlog-modal-breadcrumb">
              <button type="button" onClick={onBack} disabled={breadcrumb.length === 0}>
                Back
              </button>
              {breadcrumb.map((id) => (
                <button type="button" key={id} onClick={() => onSelectRelated(id)}>
                  {id}
                </button>
              ))}
            </div>
            <h3 id="backlog-modal-title">{title}</h3>
            <div className="cell-mono-id">{fallbackBugId}</div>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close backlog detail">
            x
          </button>
        </div>

        {loadingBug ? <div className="timeline-empty">Loading backlog detail...</div> : null}
        {error ? <div className="timeline-empty timeline-error">Backlog detail load failed: {error}</div> : null}
        {bug ? <BacklogDetailSummary bug={bug} gate={gate} /> : null}

        <div className="backlog-modal-tabs" role="tablist" aria-label="Backlog detail sections">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "timeline"}
            className={activeTab === "timeline" ? "active" : ""}
            onClick={() => setActiveTab("timeline")}
          >
            Timeline
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "contract"}
            className={activeTab === "contract" ? "active" : ""}
            onClick={() => setActiveTab("contract")}
          >
            Contract & Gate
          </button>
        </div>

        {activeTab === "timeline" ? (
          <div className="backlog-modal-tab-panel" role="tabpanel">
            <div className="backlog-modal-section">
              <div className="backlog-modal-section-head">
                <span>Related backlog</span>
                <span className="mono">{dag.relatedIds.length} discovered</span>
              </div>
              {dag.relatedIds.length > 0 ? (
                <div className="backlog-relation-strip">
                  {dag.relatedIds.map((id) => (
                    <button type="button" key={id} onClick={() => onSelectRelated(id)}>
                      {id}
                    </button>
                  ))}
                </div>
              ) : (
                <div className="timeline-empty">No related backlog ids discovered from provenance, contract, timeline, or task fields.</div>
              )}
            </div>

            <div className="backlog-modal-section">
              <div className="backlog-modal-section-head">
                <span>Timeline DAG</span>
                <span className="mono">
                  {dag.nodes.length} node{dag.nodes.length === 1 ? "" : "s"} · {dag.phaseLabels.length} phase{dag.phaseLabels.length === 1 ? "" : "s"}
                  {dag.workerLaneCount > 1 ? ` · ${dag.workerLaneCount} workers parallel` : ""}
                </span>
              </div>
              {timeline?.loading ? <div className="timeline-empty">Loading timeline...</div> : null}
              {timeline?.error ? <div className="timeline-empty timeline-error">Timeline load failed: {timeline.error}</div> : null}
              {!timeline?.loading && !timeline?.error && dag.nodes.length === 0 ? (
                <div className="timeline-empty">No execution timeline events are available.</div>
              ) : null}
              {dag.nodes.length > 0 ? (
                <div className="backlog-dag-shell">
                  <div className="backlog-dag-phases" style={{ gridTemplateColumns: `120px repeat(${dag.phaseLabels.length}, minmax(130px, 1fr))` }}>
                    <span />
                    {dag.phaseLabels.map((phase) => (
                      <span key={phase}>{phase}</span>
                    ))}
                  </div>
                  <div className="backlog-dag-grid">
                    {dag.lanes.map((lane) => (
                      <div className={`backlog-dag-lane lane-${cssToken(lane.id)}`} key={lane.id}>
                        <div className="backlog-dag-lane-label" title={lane.family === "worker" ? "Subagents / Workers" : "Observer"}>
                          {lane.label}
                        </div>
                        <div className="backlog-dag-lane-track" style={{ gridTemplateColumns: `repeat(${dag.phaseLabels.length}, minmax(130px, 1fr))` }}>
                          {lane.nodes.map((node) => (
                            <button
                              type="button"
                              key={node.id}
                              className={`backlog-dag-node status-${node.status} ${selectedNode?.id === node.id ? "selected" : ""}`}
                              style={{ gridColumn: `${node.phaseIndex + 1} / span 1` }}
                              onClick={() => setSelectedNodeId(node.id)}
                              title={node.inferred ? "Lane/phase inferred from event fields" : node.label}
                            >
                              <span>{node.label}</span>
                              <em>{node.statusLabel}</em>
                              {node.inferred ? <strong>inferred</strong> : null}
                            </button>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>

            <EvidenceInspector node={selectedNode} />
          </div>
        ) : (
          <ContractGatePanel audit={contractAudit} response={timeline?.gate} gate={gate} />
        )}
      </section>
    </div>
  );
}

function BacklogDetailSummary({ bug, gate }: { bug: BacklogBug; gate?: MfCloseTimelineGate }) {
  const contract = gate?.contract_gate;
  const missing = stableUnique([...(gate?.missing_event_kinds ?? []), ...(contract?.missing_requirement_ids ?? [])]);
  const related = relatedIdsFromBug(bug, []);
  return (
    <div className="backlog-modal-summary">
      <SummaryItem label="Priority" value={normalizePriority(bug.priority)} tone={priorityTone(bug.priority)} />
      <SummaryItem label="Status" value={normalizeStatus(bug.status)} tone={statusClass(bug.status)} />
      <SummaryItem label="Commit" value={bug.commit ? shortCommit(bug.commit) : "none"} mono />
      <SummaryItem label="Contract" value={contract?.status || (bug.contract_summary?.has_contract ? "declared" : "not declared")} tone={contract?.passed ? "status-complete" : contract ? "status-failed" : "status-unknown"} />
      <SummaryItem label="Close gate" value={gate?.status || (gate ? (gate.passed ? "passed" : "blocked") : "not loaded")} tone={gate?.passed ? "status-complete" : gate ? "status-failed" : "status-unknown"} />
      <SummaryItem label="Missing" value={missing.length ? String(missing.length) : "none"} tone={missing.length ? "status-failed" : "status-complete"} />
      <DetailList label="Target files" values={listFrom(bug.target_files)} />
      <DetailList label="Tests" values={listFrom(bug.test_files)} />
      <DetailList label="Required docs" values={listFrom(bug.required_docs)} />
      <DetailList label="Provenance / related" values={related} />
    </div>
  );
}

function SummaryItem({ label, value, tone, mono = false }: { label: string; value: string; tone?: string; mono?: boolean }) {
  return (
    <div className="backlog-summary-item">
      <span>{label}</span>
      <strong className={`${mono ? "mono" : ""} ${tone ?? ""}`}>{value}</strong>
    </div>
  );
}

function DetailList({ label, values }: { label: string; values: string[] }) {
  return (
    <div className="backlog-summary-list">
      <span>{label}</span>
      <div>
        {(values.length > 0 ? values : ["none"]).slice(0, 8).map((value) => (
          <em key={value} className="mono">{value}</em>
        ))}
        {values.length > 8 ? <strong>+{values.length - 8}</strong> : null}
      </div>
    </div>
  );
}

function ImplementationStepGrid({ steps, observerEventCount }: { steps: ImplementationStep[]; observerEventCount: number }) {
  return (
    <div className="backlog-implementation-steps" aria-label="Implementation audit steps">
      <div className="backlog-modal-section-head">
        <span>Implementation steps</span>
        <span className="mono">
          {steps.length} step{steps.length === 1 ? "" : "s"}
          {observerEventCount ? ` · observer supervision compacted ${observerEventCount}` : ""}
        </span>
      </div>
      <div className="backlog-step-grid">
        {steps.map((step) => (
          <div className={`backlog-step-card status-${step.status} ${step.coarse ? "coarse" : ""}`} key={step.id}>
            <div className="backlog-step-head">
              <span>{step.label}</span>
              <span className={`status-badge ${statusClass(step.status)}`}>{step.coarse ? "coarse" : step.status}</span>
            </div>
            <div className="backlog-gate-facts">
              <span>{step.events.length} event{step.events.length === 1 ? "" : "s"}</span>
              {step.coarse ? <span>coarse/inferred from available fields</span> : null}
            </div>
            <ArtifactPills summary={step.artifacts} compact />
          </div>
        ))}
      </div>
    </div>
  );
}

function ArtifactPills({ summary, compact = false }: { summary: EventArtifactSummary; compact?: boolean }) {
  const groups = [
    ["graph traces", summary.graphTraceIds],
    ["nodes", stableUnique([...summary.nodeIds, ...summary.nodeTitles])],
    ["files", summary.changedFiles],
    ["tests", summary.tests],
    ["docs/config", summary.docs],
    ["screenshots", summary.screenshots],
    ["precheck/gate", summary.prechecks],
  ].filter(([, values]) => Array.isArray(values) && values.length > 0) as [string, string[]][];
  if (groups.length === 0) return null;
  return (
    <div className={`backlog-artifact-pills ${compact ? "compact" : ""}`}>
      {groups.map(([label, values]) => (
        <div key={label}>
          <span>{label}</span>
          {values.slice(0, compact ? 3 : 8).map((value) => (
            <em key={value} className="mono" title={value}>{value}</em>
          ))}
          {values.length > (compact ? 3 : 8) ? <strong>+{values.length - (compact ? 3 : 8)}</strong> : null}
        </div>
      ))}
    </div>
  );
}

function EvidenceInspector({ node }: { node: TimelineDagNode | null }) {
  const event = node?.event;
  const artifacts = event ? timelineEventArtifacts(event) : emptyArtifactSummary();
  const rows = [
    ["event_type", event?.event_type],
    ["event_kind", event?.event_kind],
    ["actor", event?.actor],
    ["display_actor", event ? displayActorForEvent(event) : ""],
    ["raw_lane", event ? rawLaneKeyForEvent(event) : node?.rawLane],
    ["phase", event?.phase],
    ["status", event?.status ?? node?.statusLabel],
    ["created_at", event?.created_at],
    ["commit_sha", event?.commit_sha],
    ["task_id", event?.task_id],
    ["attempt_num", event?.attempt_num],
  ];
  return (
    <div className="backlog-evidence-inspector">
      <div className="backlog-modal-section-head">
        <span>Evidence inspector</span>
        <span className="mono">{node?.id ?? "no node"}</span>
      </div>
      {node ? (
        <>
          <div className="backlog-inspector-grid">
            {rows.map(([key, value]) => (
              <div key={key}>
                <span>{key}</span>
                <strong className="mono">{value == null || value === "" ? "-" : String(value)}</strong>
              </div>
            ))}
          </div>
          <ArtifactPills summary={artifacts} />
          <div className="backlog-inspector-json">
            <div>
              <span>verification</span>
              <pre>{JSON.stringify(event?.verification ?? node.syntheticVerification ?? {}, null, 2)}</pre>
            </div>
            <div>
              <span>artifact_refs</span>
              <pre>{JSON.stringify(event?.artifact_refs ?? {}, null, 2)}</pre>
            </div>
            <div>
              <span>raw payload</span>
              <pre>{JSON.stringify(event?.payload ?? node.syntheticPayload ?? {}, null, 2)}</pre>
            </div>
          </div>
        </>
      ) : (
        <div className="timeline-empty">Select a timeline or contract node to inspect evidence.</div>
      )}
    </div>
  );
}

function ContractGatePanel({
  audit,
  response,
  gate,
}: {
  audit: ContractAudit;
  response?: BacklogTimelineGateResponse;
  gate?: MfCloseTimelineGate;
}) {
  const gateState = gateEvidenceState(response, audit.events);
  return (
    <div className="backlog-modal-tab-panel" role="tabpanel">
      <div className="backlog-modal-section">
        <div className="backlog-modal-section-head">
          <span>Original contract inputs</span>
          <span className={`status-badge ${audit.contract.valid ? "status-complete" : audit.contract.empty ? "status-unknown" : "status-failed"}`}>
            {audit.contract.empty ? "missing" : audit.contract.valid ? "parsed" : "invalid json"}
          </span>
        </div>
        {audit.contract.error ? <div className="timeline-empty timeline-error">{audit.contract.error}</div> : null}
        <div className="backlog-contract-inputs">
          {audit.inputs.map((item) => (
            <div key={item.label}>
              <span>{item.label}</span>
              <strong className={item.mono ? "mono" : ""}>{item.value}</strong>
            </div>
          ))}
        </div>
      </div>

      <div className="backlog-modal-section">
        <div className="backlog-modal-section-head">
          <span>Requirement evidence map</span>
          <span className="mono">{audit.requirements.length} requirement{audit.requirements.length === 1 ? "" : "s"}</span>
        </div>
        {audit.requirements.length === 0 ? (
          <div className="timeline-empty">No contract evidence requirements were declared.</div>
        ) : (
          <div className="backlog-contract-requirements">
            {audit.requirements.map((requirement) => (
              <ContractRequirementCard key={requirement.id} requirement={requirement} />
            ))}
          </div>
        )}
      </div>

      <div className="backlog-modal-section">
        <div className="backlog-modal-section-head">
          <span>Gate evidence</span>
          <span className={`status-badge ${gateState.noGate ? "status-unknown" : gate?.passed ? "status-complete" : "status-failed"}`}>
            {gateState.noGate ? "no gate evidence" : gate?.status || "recorded"}
          </span>
        </div>
        {gateState.noGate ? <NoGateNotice reason={gateState.reason} /> : gate ? <GateSummary gate={gate} response={response} /> : null}
      </div>

      <div className="backlog-modal-section">
        <div className="backlog-modal-section-head">
          <span>Raw contract / gate payloads</span>
          <span className="mono">inspectable</span>
        </div>
        <div className="backlog-raw-payload-grid">
          <RawPayloadBlock label="chain_trigger_json raw" value={audit.contract.rawForDisplay} />
          <RawPayloadBlock label="parsed contract root" value={audit.contract.valid ? audit.contract.root : { error: audit.contract.error || "missing" }} />
          <RawPayloadBlock label="timeline gate raw" value={response ?? { state: "not recorded" }} />
        </div>
      </div>
    </div>
  );
}

function ContractRequirementCard({ requirement }: { requirement: ContractRequirementAudit }) {
  return (
    <div className={`backlog-contract-requirement status-${requirement.status} ${requirement.coarse ? "coarse" : ""}`}>
      <div className="backlog-contract-requirement-head">
        <span className="mono">{requirement.id}</span>
        <span className={`status-badge ${contractStatusClass(requirement.status)}`}>{requirement.status.replace("_", " ")}</span>
      </div>
      <div className="backlog-gate-facts">
        <span>{requirement.required ? "required" : "optional"}</span>
        <span>{requirement.source}</span>
        {requirement.coarse ? <span>coarse/inferred</span> : null}
      </div>
      {requirement.description ? <p>{requirement.description}</p> : null}
      {requirement.evidence.length > 0 ? (
        <div className="backlog-contract-evidence-list">
          {requirement.evidence.map((match) => (
            <div key={match.key} className={`backlog-contract-evidence status-${match.status}`}>
              <div>
                <strong>{match.label}</strong>
                <span className="mono">{match.actor || "actor unknown"} · {match.phase || match.eventKind || "event"}</span>
              </div>
              <span className={`status-badge ${contractStatusClass(match.status)}`}>{match.status.replace("_", " ")}</span>
              <ArtifactPills summary={match.artifacts} compact />
            </div>
          ))}
        </div>
      ) : (
        <div className="timeline-empty">No matching timeline evidence recorded for this requirement.</div>
      )}
    </div>
  );
}

function RawPayloadBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="backlog-raw-payload">
      <span>{label}</span>
      <pre>{typeof value === "string" ? value || "missing" : JSON.stringify(value ?? { state: "missing" }, null, 2)}</pre>
    </div>
  );
}

function NoGateNotice({ reason }: { reason: string }) {
  return (
    <div className="backlog-no-gate-state">
      <strong>No gate evidence</strong>
      <span>{reason || "No gate run recorded for this backlog/stage. Contract requirements remain visible without fabricated gate nodes."}</span>
    </div>
  );
}

function buildContractAudit(
  bug: BacklogBug | null,
  events: TaskTimelineEvent[],
  gate?: MfCloseTimelineGate,
  response?: BacklogTimelineGateResponse,
): ContractAudit {
  const contract = parseContract(bug?.chain_trigger_json);
  const root = contract.root;
  const requirements = contractRequirements(root, gate);
  const inputs = contractInputs(bug, root, requirements.length);
  const contractGate = gate?.contract_gate;
  const present = new Set(contractGate?.present_requirement_ids ?? []);
  const missing = new Set(contractGate?.missing_requirement_ids ?? []);
  const noGate = gateEvidenceState(response, events).noGate;
  return {
    contract,
    inputs,
    events,
    requirements: requirements.map((requirement) => {
      const evidence = events
        .map((event, index) => contractEvidenceMatch(event, index, requirement.id))
        .filter((item): item is ContractEvidenceMatch => Boolean(item));
      const evidenceStatuses = evidence.map((item) => item.status);
      let status: ContractEvidenceStatus = "missing";
      if (response?.applicable === false) status = "not_applicable";
      else if (evidenceStatuses.includes("passed")) status = "passed";
      else if (evidenceStatuses.includes("failed")) status = "failed";
      else if (evidenceStatuses.includes("bypassed")) status = "bypassed";
      else if (evidenceStatuses.includes("inferred")) status = "inferred";
      else if (missing.has(requirement.id)) status = "missing";
      else if (present.has(requirement.id)) status = "inferred";
      else if (!requirement.required && noGate) status = "not_applicable";
      return {
        ...requirement,
        status,
        coarse: status === "inferred" || evidence.some((item) => item.status === "inferred"),
        evidence,
      };
    }),
  };
}

function parseContract(value: BacklogBug["chain_trigger_json"] | undefined): ParsedContract {
  if (value == null || value === "") {
    return { valid: true, empty: true, error: "", root: {}, rawForDisplay: "missing" };
  }
  if (typeof value === "object") {
    const root = contractRoot(asRecord(value));
    return { valid: true, empty: Object.keys(root).length === 0, error: "", root, rawForDisplay: value };
  }
  const text = String(value).trim();
  if (!text) return { valid: true, empty: true, error: "", root: {}, rawForDisplay: "missing" };
  try {
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { valid: false, empty: false, error: "Contract JSON parsed but is not an object.", root: {}, rawForDisplay: text };
    }
    const root = contractRoot(parsed as Record<string, unknown>);
    return { valid: true, empty: Object.keys(root).length === 0, error: "", root, rawForDisplay: text };
  } catch (error) {
    return { valid: false, empty: false, error: `Invalid contract JSON: ${error instanceof Error ? error.message : String(error)}`, root: {}, rawForDisplay: text };
  }
}

function contractRoot(value: Record<string, unknown>): Record<string, unknown> {
  const parallel = asRecord(value.parallel_contract);
  return Object.keys(parallel).length > 0 ? parallel : value;
}

function contractInputs(bug: BacklogBug | null, root: Record<string, unknown>, requirementCount: number): ContractInputItem[] {
  const criteria = listFrom(bug?.acceptance_criteria);
  const targetFiles = listFrom(bug?.target_files);
  const testFiles = listFrom(bug?.test_files);
  const requiredDocs = listFrom(bug?.required_docs);
  return [
    { label: "template", value: stringField(root, "template_id") || "none", mono: true },
    { label: "instance", value: stringField(root, "contract_instance_id") || bug?.bug_id || "none", mono: true },
    { label: "mode", value: stringField(root, "mode") || "unspecified" },
    { label: "required evidence", value: String(requirementCount), mono: true },
    { label: "acceptance criteria", value: String(criteria.length), mono: true },
    { label: "target files", value: targetFiles.length ? targetFiles.slice(0, 3).join(", ") : "none", mono: true },
    { label: "test files", value: testFiles.length ? testFiles.slice(0, 3).join(", ") : "none", mono: true },
    { label: "required docs", value: requiredDocs.length ? requiredDocs.slice(0, 3).join(", ") : "none", mono: true },
  ];
}

function contractRequirements(root: Record<string, unknown>, gate?: MfCloseTimelineGate): Omit<ContractRequirementAudit, "status" | "coarse" | "evidence">[] {
  const requirements: Omit<ContractRequirementAudit, "status" | "coarse" | "evidence">[] = [];
  const add = (item: unknown, required: boolean, source: string) => {
    const normalized = normalizeContractRequirement(item, required, source);
    if (!normalized) return;
    const existing = requirements.find((requirement) => requirement.id === normalized.id);
    if (existing) {
      existing.required = existing.required || normalized.required;
      if (!existing.description && normalized.description) existing.description = normalized.description;
      if (!existing.label && normalized.label) existing.label = normalized.label;
      return;
    }
    requirements.push(normalized);
  };
  listUnknown(root.evidence_requirements).forEach((item) => add(item, true, "evidence_requirements"));
  listUnknown(root.required_evidence).forEach((item) => add(item, true, "required_evidence"));
  const integration = asRecord(root.integration);
  listUnknown(integration.required_evidence).forEach((item) => add(item, true, "integration.required_evidence"));
  listUnknown(integration.optional_evidence).forEach((item) => add(item, false, "integration.optional_evidence"));
  const e2e = asRecord(root.e2e_contract);
  if (e2e.required) add({ ...e2e, id: e2e.requirement_id || "e2e", label: e2e.label || "E2E" }, true, "e2e_contract");
  const testPolicy = asRecord(root.test_scenario_policy);
  listUnknown(testPolicy.required_evidence_ids).forEach((id) => add({ id, kind: "test_scenario_policy" }, true, "test_scenario_policy"));
  for (const id of gate?.contract_gate?.required_requirement_ids ?? []) add({ id }, true, "contract_gate.required_requirement_ids");
  for (const id of gate?.contract_gate?.optional_requirement_ids ?? []) add({ id }, false, "contract_gate.optional_requirement_ids");
  return requirements;
}

function normalizeContractRequirement(
  item: unknown,
  defaultRequired: boolean,
  source: string,
): Omit<ContractRequirementAudit, "status" | "coarse" | "evidence"> | null {
  const record = asRecord(item);
  const rawId =
    stringField(record, "id") ||
    stringField(record, "requirement_id") ||
    stringField(record, "contract_requirement_id") ||
    stringField(record, "name") ||
    (typeof item === "string" ? item : "");
  const id = rawId.trim();
  if (!id) return null;
  return {
    id,
    label: stringField(record, "label") || id,
    description: stringField(record, "description") || stringField(record, "summary") || stringField(record, "command"),
    required: typeof record.required === "boolean" ? record.required : defaultRequired,
    source,
  };
}

function contractEvidenceMatch(event: TaskTimelineEvent, index: number, requirementId: string): ContractEvidenceMatch | null {
  const ids = evidenceRequirementIds(event);
  if (!ids.includes(requirementId)) return null;
  const explicitStatus = explicitContractEvidenceStatus(event, requirementId);
  const status = explicitStatus || contractStatusForEvent(event);
  return {
    key: `${timelineEventKey(event, index)}:${requirementId}`,
    label: contractEvidenceLabel(event, index, requirementId),
    actor: displayActorForEvent(event),
    eventKind: event.event_kind || "",
    phase: event.phase || "",
    status,
    artifacts: timelineEventArtifacts(event),
  };
}

function explicitContractEvidenceStatus(event: TaskTimelineEvent, requirementId: string): ContractEvidenceStatus | "" {
  const containers = [asRecord(event.payload), asRecord(event.verification), asRecord(event.artifact_refs)];
  for (const container of containers) {
    for (const item of listUnknown(container.contract_evidence)) {
      const evidence = asRecord(item);
      const ids = [
        stringField(evidence, "requirement_id"),
        stringField(evidence, "id"),
        ...listUnknown(evidence.requirement_ids).map(String),
      ].filter(Boolean);
      if (ids.includes(requirementId)) return normalizeContractStatus(stringField(evidence, "status") || event.status || event.decision || "");
    }
  }
  return "";
}

function contractStatusForEvent(event: TaskTimelineEvent): ContractEvidenceStatus {
  if (isCoarseOrInferredEvent(event) && dagStatusForEvent(event) === "unknown") return "inferred";
  const status = normalizeContractStatus(event.status || event.decision || event.event_type || event.event_kind || "");
  if (status) return status;
  return eventHasConcreteArtifacts(event) ? "inferred" : "missing";
}

function normalizeContractStatus(value: string): ContractEvidenceStatus | "" {
  const text = value.toLowerCase();
  if (!text) return "";
  if (text.includes("bypass") || text.includes("waive")) return "bypassed";
  if (text.includes("fail") || text.includes("block") || text.includes("reject")) return "failed";
  if (text.includes("infer") || text.includes("coarse")) return "inferred";
  if (text.includes("not_applicable") || text.includes("not applicable") || text.includes("n/a")) return "not_applicable";
  if (text.includes("pass") || text.includes("accept") || text.includes("success") || text.includes("ok") || text.includes("succeed")) return "passed";
  return "";
}

function contractStatusClass(status: ContractEvidenceStatus): string {
  if (status === "passed") return "status-complete";
  if (status === "failed" || status === "missing") return "status-failed";
  if (status === "bypassed" || status === "inferred") return "status-running";
  return "status-unknown";
}

function gateEvidenceState(response: BacklogTimelineGateResponse | undefined, events: TaskTimelineEvent[]): { noGate: boolean; reason: string } {
  if (!response) return { noGate: true, reason: "No gate precheck response has been loaded." };
  const gate = response.timeline_gate;
  if (response.applicable === false) {
    return { noGate: true, reason: response.reason || "Backlog/stage is not subject to the MF close gate." };
  }
  const contractEvents = gate?.contract_gate?.evidence_events ?? [];
  const presentKinds = gate?.present_event_kinds ?? [];
  const ignored = gate?.ignored_required_events ?? [];
  const hasRealEvidence = events.length > 0 || contractEvents.length > 0 || presentKinds.length > 0 || ignored.length > 0;
  return {
    noGate: !hasRealEvidence,
    reason: hasRealEvidence ? "" : "No gate run recorded and no timeline evidence is available yet.",
  };
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

interface TimelineDag {
  lanes: TimelineDagLane[];
  nodes: TimelineDagNode[];
  phaseLabels: string[];
  relatedIds: string[];
  workerLaneCount: number;
}

interface TimelineDagLane {
  id: string;
  label: string;
  nodes: TimelineDagNode[];
  family: "observer" | "worker";
}

interface TimelineDagNode {
  id: string;
  label: string;
  lane: string;
  rawLane: string;
  phase: string;
  phaseIndex: number;
  status: "passed" | "missing" | "failed" | "retry" | "bypassed" | "running" | "unknown";
  statusLabel: string;
  event?: TaskTimelineEvent;
  inferred?: boolean;
  syntheticPayload?: Record<string, unknown>;
  syntheticVerification?: Record<string, unknown>;
}

export function buildBacklogParallelTimelineFixtureDagForTest(): TimelineDag {
  return buildTimelineDag(
    {
      bug_id: "FIXTURE-BACKLOG-PARALLEL-TIMELINE",
      title: "Fixture backlog parallel timeline",
      status: "OPEN",
      priority: "P0",
      provenance_paths: ["FIXTURE-RELATED-BACKLOG"],
    },
    BACKLOG_PARALLEL_TIMELINE_FIXTURE_EVENTS,
    {
      passed: false,
      status: "blocked",
      required_event_kinds: ["implementation", "verification", "close_ready"],
      present_event_kinds: ["implementation", "verification"],
      missing_event_kinds: ["close_ready"],
      contract_gate: {
        passed: false,
        status: "blocked",
        required_requirement_ids: ["parallel_timeline_dag", "evidence_inspector", "contract_missing_visualization"],
        present_requirement_ids: ["parallel_timeline_dag", "evidence_inspector"],
        missing_requirement_ids: ["contract_missing_visualization"],
      },
    },
  );
}

function buildTimelineDag(bug: BacklogBug | null, events: TaskTimelineEvent[], _gate?: MfCloseTimelineGate): TimelineDag {
  const phaseLabels: string[] = [];
  const nodes: TimelineDagNode[] = [];
  const orderedEvents = events.slice().sort(compareTimelineEvents);
  const laneContext = buildTimelineLaneContext(orderedEvents);
  orderedEvents.forEach((event, index) => {
    const phase = phaseLabelForEvent(event, index);
    const lane = timelineLaneIdForEvent(event, laneContext);
    const eventNode: TimelineDagNode = {
      id: `event:${timelineEventKey(event, index)}`,
      label: timelineNodeLabel(event, index),
      lane,
      rawLane: rawLaneKeyForEvent(event),
      phase,
      phaseIndex: phaseIndex(phaseLabels, phase),
      status: dagStatusForEvent(event),
      statusLabel: event.status || event.decision || event.event_kind || "event",
      event,
      inferred: isInferredLane(event),
    };
    nodes.push(eventNode);
  });

  const laneOrder = timelineLaneOrder(events);
  const laneMap = new Map<string, TimelineDagNode[]>();
  for (const node of nodes) {
    laneMap.set(node.lane, [...(laneMap.get(node.lane) ?? []), node]);
  }
  const lanes = Array.from(laneMap.entries())
    .map(([id, laneNodes]) => ({
      id,
      label: timelineLaneDisplayLabel(id, laneContext),
      nodes: laneNodes.sort((a, b) => a.phaseIndex - b.phaseIndex || a.label.localeCompare(b.label)),
      family: id.startsWith("worker") ? "worker" as const : "observer" as const,
    }))
    .sort((a, b) => {
      const ai = laneOrder.indexOf(a.id);
      const bi = laneOrder.indexOf(b.id);
      if (ai !== bi) return (ai < 0 ? laneOrder.length : ai) - (bi < 0 ? laneOrder.length : bi);
      return a.id.localeCompare(b.id);
    });

  return {
    lanes,
    nodes,
    phaseLabels,
    relatedIds: relatedIdsFromBug(bug, events),
    workerLaneCount: laneContext.workerKeys.length,
  };
}

function phaseIndex(phases: string[], phase: string): number {
  const existing = phases.indexOf(phase);
  if (existing >= 0) return existing;
  phases.push(phase);
  return phases.length - 1;
}

function phaseLabelForEvent(event: TaskTimelineEvent, index: number): string {
  const payload = asRecord(event.payload);
  const explicit = event.phase || stringField(payload, "phase") || stringField(payload, "stage");
  if (explicit) return explicit.replace(/_/g, " ");
  if (event.event_kind) return event.event_kind.replace(/_/g, " ");
  return `event ${index + 1}`;
}

function dagStatusForEvent(event: TaskTimelineEvent): TimelineDagNode["status"] {
  const text = [event.status, event.decision, event.event_type, event.event_kind, event.phase].join(" ").toLowerCase();
  if (text.includes("retry")) return "retry";
  if (text.includes("bypass") || text.includes("waived")) return "bypassed";
  if (text.includes("fail") || text.includes("blocked") || text.includes("reject")) return "failed";
  if (text.includes("running") || text.includes("claimed") || text.includes("pending")) return "running";
  if (text.includes("pass") || text.includes("accept") || text.includes("success") || text.includes("ok") || text.includes("succeed")) return "passed";
  return "unknown";
}

function evidenceRequirementIds(event: TaskTimelineEvent): string[] {
  const payload = asRecord(event.payload);
  const verification = asRecord(event.verification);
  const contractEvidence = listUnknown(verification.contract_evidence).flatMap((item) => {
    const record = asRecord(item);
    return [stringField(record, "requirement_id"), stringField(record, "id"), ...listUnknown(record.requirement_ids).map(String)];
  });
  return stableUnique([
    stringField(payload, "requirement_id"),
    ...listUnknown(payload.requirement_ids).map(String),
    ...listUnknown(payload.requirement_id).map(String),
    stringField(verification, "requirement_id"),
    ...listUnknown(verification.requirement_ids).map(String),
    ...listUnknown(verification.requirement_id).map(String),
    ...contractEvidence,
  ].map((value) => value.trim()).filter(Boolean));
}

function isInferredLane(event: TaskTimelineEvent): boolean {
  const payload = asRecord(event.payload);
  const verification = asRecord(event.verification);
  return !(
    stringField(payload, "lane") ||
    stringField(payload, "agent_lane") ||
    stringField(payload, "worker_lane") ||
    stringField(payload, "agent_id") ||
    stringField(payload, "parallel_agent_id") ||
    stringField(verification, "lane")
  );
}

function relatedIdsFromBug(bug: BacklogBug | null, events: TaskTimelineEvent[]): string[] {
  const values: unknown[] = [];
  if (bug) {
    values.push(bug.provenance_paths, bug.chain_trigger_json, bug.current_task_id, bug.root_task_id, bug.bug_id);
  }
  for (const event of events) {
    values.push(
      event.backlog_id,
      event.task_id,
      event.trace_id,
      event.correlation_id,
      event.payload,
      event.verification,
      event.artifact_refs,
    );
  }
  return stableUnique(collectBacklogIds(values)).filter((id) => id !== bug?.bug_id);
}

function collectBacklogIds(values: unknown[]): string[] {
  const ids: string[] = [];
  const visit = (value: unknown) => {
    if (value == null) return;
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (typeof value === "object") {
      Object.entries(value as Record<string, unknown>).forEach(([key, item]) => {
        if (/backlog|bug|related|parent|child|provenance|correlation|trace|task/i.test(key)) visit(item);
      });
      return;
    }
    const text = String(value);
    for (const match of text.matchAll(/\b[A-Z][A-Z0-9]+(?:-[A-Z0-9]+){2,}\b/g)) {
      ids.push(match[0]);
    }
  };
  values.forEach(visit);
  return ids;
}

function compareTimelineEvents(a: TaskTimelineEvent, b: TaskTimelineEvent): number {
  const at = dateValue(a.created_at);
  const bt = dateValue(b.created_at);
  if (at !== bt) return at - bt;
  return Number(a.id ?? 0) - Number(b.id ?? 0);
}

function buildTimelineLaneContext(events: TaskTimelineEvent[]): TimelineLaneContext {
  const workerKeys = stableUnique(events.filter(isWorkerTimelineEvent).map(rawWorkerKeyForEvent).filter(Boolean));
  const roleCounts = new Map<string, number>();
  const workerAliases = new Map<string, string>();
  workerKeys.forEach((key, index) => {
    const event = events.find((item) => rawWorkerKeyForEvent(item) === key);
    const role = event ? workerRoleForEvent(event) : "";
    const count = role ? (roleCounts.get(role) ?? 0) + 1 : 0;
    if (role) roleCounts.set(role, count);
    const roleLabel = role ? `${titleizeLane(role)} worker${count > 1 ? ` ${count}` : ""}` : "";
    workerAliases.set(key, roleLabel || `Worker ${index + 1}`);
  });
  return { workerKeys, workerAliases };
}

function timelineLaneIdForEvent(event: TaskTimelineEvent, context: TimelineLaneContext): string {
  if (!isWorkerTimelineEvent(event)) return "observer";
  if (context.workerKeys.length <= 1) return "worker";
  const key = rawWorkerKeyForEvent(event);
  if (!key) return "worker";
  return `worker_${cssToken(context.workerAliases.get(key) || key)}`;
}

function timelineLaneDisplayLabel(id: string, context: TimelineLaneContext): string {
  if (id === "observer") return "Observer";
  if (id === "worker") return "Subagents / Workers";
  for (const alias of context.workerAliases.values()) {
    if (id === `worker_${cssToken(alias)}`) return `Subagents / Workers · ${alias}`;
  }
  return id.startsWith("worker_") ? `Subagents / Workers · ${titleizeLane(id.replace(/^worker_/, ""))}` : titleizeLane(id);
}

function rawWorkerKeyForEvent(event: TaskTimelineEvent): string {
  const payload = asRecord(event.payload);
  const verification = asRecord(event.verification);
  return (
    stringField(payload, "worker_id") ||
    stringField(payload, "agent_id") ||
    stringField(payload, "parallel_agent_id") ||
    stringField(payload, "subagent_id") ||
    stringField(verification, "worker_id") ||
    stringField(verification, "agent_id") ||
    event.actor ||
    event.task_id ||
    event.trace_id ||
    ""
  );
}

function rawLaneKeyForEvent(event: TaskTimelineEvent): string {
  const payload = asRecord(event.payload);
  const verification = asRecord(event.verification);
  return (
    stringField(payload, "lane") ||
    stringField(payload, "agent_lane") ||
    stringField(payload, "worker_lane") ||
    stringField(payload, "agent_id") ||
    stringField(payload, "parallel_agent_id") ||
    stringField(payload, "worker_id") ||
    stringField(verification, "lane") ||
    stringField(verification, "agent_id") ||
    stringField(verification, "worker_id") ||
    event.actor ||
    event.phase ||
    event.event_kind ||
    event.event_type ||
    "event"
  );
}

function workerRoleForEvent(event: TaskTimelineEvent): string {
  const text = rawLaneKeyForEvent(event).toLowerCase();
  if (text.includes("front")) return "frontend";
  if (text.includes("back")) return "backend";
  if (text.includes("test") || text.includes("qa") || text.includes("verify")) return "verification";
  if (text.includes("doc")) return "docs";
  return "";
}

function isWorkerTimelineEvent(event: TaskTimelineEvent): boolean {
  const text = eventSearchText(event);
  const lane = rawLaneKeyForEvent(event).toLowerCase();
  if (text.includes("observer") && !text.includes("subagent") && !text.includes("worker") && !text.includes("mf_sub")) return false;
  return (
    event.event_kind === "implementation" ||
    lane.includes("subagent") ||
    lane.includes("worker") ||
    lane.includes("mf_sub") ||
    lane.includes("front") ||
    lane.includes("back") ||
    text.includes("subagent") ||
    text.includes("mf_sub") ||
    text.includes("changed_files")
  );
}

function displayActorForEvent(event: TaskTimelineEvent): string {
  if (!event.actor) return "actor unknown";
  if (isWorkerTimelineEvent(event)) return "Subagent worker";
  if (event.actor.toLowerCase().includes("observer")) return "Observer";
  return titleizeLane(event.actor.replace(/[^a-zA-Z0-9]+/g, "_"));
}

function timelineNodeLabel(event: TaskTimelineEvent, index: number): string {
  if ((event.event_type || "").toLowerCase() === "contract_evidence") {
    const ids = evidenceRequirementIds(event);
    return ids.length > 0 ? `contract_evidence · ${ids[0]}` : "contract_evidence";
  }
  const ids = evidenceRequirementIds(event);
  const base = event.event_type || event.event_kind || `event ${index + 1}`;
  return base === "contract_evidence" || ids.length === 0 ? base : `${base}`;
}

function contractEvidenceLabel(event: TaskTimelineEvent, index: number, requirementId: string): string {
  const base = event.event_type || event.event_kind || `event ${index + 1}`;
  return `${base} · ${requirementId}`;
}

interface TimelineLane {
  id: string;
  label: string;
  events: TaskTimelineEvent[];
  latestStatus: string;
  latestActor: string;
  latestActorRaw: string;
  latestCommit: string;
  blockers: string[];
  deemphasized?: boolean;
}

interface TimelineLaneContext {
  workerKeys: string[];
  workerAliases: Map<string, string>;
}

interface EventArtifactSummary {
  graphTraceIds: string[];
  nodeIds: string[];
  nodeTitles: string[];
  changedFiles: string[];
  tests: string[];
  docs: string[];
  screenshots: string[];
  prechecks: string[];
}

interface ImplementationStep {
  id: string;
  label: string;
  events: TaskTimelineEvent[];
  status: TimelineDagNode["status"];
  artifacts: EventArtifactSummary;
  coarse: boolean;
}

interface ParsedContract {
  valid: boolean;
  empty: boolean;
  error: string;
  root: Record<string, unknown>;
  rawForDisplay: unknown;
}

interface ContractInputItem {
  label: string;
  value: string;
  mono?: boolean;
}

interface ContractAudit {
  contract: ParsedContract;
  inputs: ContractInputItem[];
  requirements: ContractRequirementAudit[];
  events: TaskTimelineEvent[];
}

interface ContractRequirementAudit {
  id: string;
  label: string;
  description: string;
  required: boolean;
  source: string;
  status: ContractEvidenceStatus;
  coarse: boolean;
  evidence: ContractEvidenceMatch[];
}

interface ContractEvidenceMatch {
  key: string;
  label: string;
  actor: string;
  eventKind: string;
  phase: string;
  status: ContractEvidenceStatus;
  artifacts: EventArtifactSummary;
}

function buildTimelineLanes(events: TaskTimelineEvent[]): TimelineLane[] {
  const grouped = new Map<string, TaskTimelineEvent[]>();
  const hasImplementation = events.some(isImplementationEvidenceEvent);
  const laneContext = buildTimelineLaneContext(events);
  for (const event of events) {
    const lane = timelineLaneIdForEvent(event, laneContext);
    grouped.set(lane, [...(grouped.get(lane) ?? []), event]);
  }
  const preferred = timelineLaneOrder(events);
  return Array.from(grouped.entries())
    .map(([id, laneEvents]) => {
      const latest = laneEvents[laneEvents.length - 1];
      return {
        id,
        label: timelineLaneDisplayLabel(id, laneContext),
        events: laneEvents,
        latestStatus: String(latest.status || latest.decision || latest.event_kind || "unknown"),
        latestActor: displayActorForEvent(latest),
        latestActorRaw: latest.actor || "",
        latestCommit: latest.commit_sha || "",
        blockers: laneEvents.flatMap((event) => eventBlockers(event)).filter(Boolean),
        deemphasized: hasImplementation && id === "observer",
      };
    })
    .sort((a, b) => {
      const ai = preferred.indexOf(a.id);
      const bi = preferred.indexOf(b.id);
      if (ai !== bi) return (ai < 0 ? preferred.length : ai) - (bi < 0 ? preferred.length : bi);
      return a.id.localeCompare(b.id);
    });
}

function timelineLaneOrder(events: TaskTimelineEvent[]): string[] {
  const hasImplementation = events.some(isImplementationEvidenceEvent);
  if (!hasImplementation) return ["observer", "worker"];
  return ["observer", "worker"];
}

function buildImplementationSteps(events: TaskTimelineEvent[]): ImplementationStep[] {
  const grouped = new Map<string, TaskTimelineEvent[]>();
  for (const event of events) {
    const stepIds = implementationStepIdsForEvent(event);
    for (const stepId of stepIds) {
      grouped.set(stepId, [...(grouped.get(stepId) ?? []), event]);
    }
  }
  const order = [
    "test_scenario",
    "graph_lookup",
    "inspected_nodes",
    "code_changes",
    "docs_config_tests",
    "verification",
    "browser_evidence",
    "merge_reconcile",
    "implementation",
  ];
  return Array.from(grouped.entries())
    .map(([id, stepEvents]) => {
      const artifacts = mergeArtifactSummaries(stepEvents.map(timelineEventArtifacts));
      return {
        id,
        label: implementationStepLabel(id),
        events: stepEvents,
        status: aggregateTimelineStatus(stepEvents),
        artifacts,
        coarse: !stepEvents.some((event) => eventHasConcreteArtifacts(event)) || stepEvents.some(isCoarseOrInferredEvent),
      };
    })
    .sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
}

function implementationStepIdsForEvent(event: TaskTimelineEvent): string[] {
  if (!isImplementationEvidenceEvent(event) && !eventHasConcreteArtifacts(event)) return [];
  const text = eventSearchText(event);
  const artifacts = timelineEventArtifacts(event);
  const steps: string[] = [];
  if (text.includes("scenario") || text.includes("test_scenario")) steps.push("test_scenario");
  if (artifacts.graphTraceIds.length > 0 || text.includes("graph query") || text.includes("graph_lookup")) steps.push("graph_lookup");
  if (artifacts.nodeIds.length > 0 || artifacts.nodeTitles.length > 0 || text.includes("inspect node")) steps.push("inspected_nodes");
  if (artifacts.changedFiles.some((file) => !isDocConfigOrTestFile(file))) steps.push("code_changes");
  if (
    artifacts.changedFiles.some(isDocConfigOrTestFile) ||
    artifacts.tests.length > 0 ||
    artifacts.docs.length > 0 ||
    text.includes("doc") ||
    text.includes("config")
  ) {
    steps.push("docs_config_tests");
  }
  if (event.event_kind === "verification" || artifacts.tests.length > 0 || artifacts.prechecks.length > 0 || text.includes("verify")) {
    steps.push("verification");
  }
  if (artifacts.screenshots.length > 0 || text.includes("browser") || text.includes("playwright") || text.includes("screenshot")) {
    steps.push("browser_evidence");
  }
  if (text.includes("merge") || text.includes("reconcile")) steps.push("merge_reconcile");
  if (steps.length === 0 && isImplementationEvidenceEvent(event)) steps.push("implementation");
  return stableUnique(steps);
}

function implementationStepLabel(stepId: string): string {
  if (stepId === "test_scenario") return "Test scenario";
  if (stepId === "graph_lookup") return "Graph lookup/query";
  if (stepId === "inspected_nodes") return "Inspected nodes";
  if (stepId === "code_changes") return "Code changes";
  if (stepId === "docs_config_tests") return "Docs/config/tests";
  if (stepId === "verification") return "Verification";
  if (stepId === "browser_evidence") return "Browser evidence";
  if (stepId === "merge_reconcile") return "Merge/reconcile";
  return "Implementation";
}

function aggregateTimelineStatus(events: TaskTimelineEvent[]): TimelineDagNode["status"] {
  const statuses = events.map(dagStatusForEvent);
  if (statuses.includes("failed")) return "failed";
  if (statuses.includes("bypassed")) return "bypassed";
  if (statuses.includes("retry")) return "retry";
  if (statuses.includes("running")) return "running";
  if (statuses.includes("passed")) return "passed";
  return "unknown";
}

function isImplementationEvidenceEvent(event: TaskTimelineEvent): boolean {
  const lane = laneLabelForEvent(event);
  const text = eventSearchText(event);
  if (lane === "observer" && !text.includes("implementation")) return false;
  return (
    event.event_kind === "implementation" ||
    lane === "worker" ||
    lane === "implementation" ||
    lane === "frontend" ||
    lane === "backend" ||
    text.includes("mf_sub") ||
    text.includes("subagent") ||
    text.includes("changed_files") ||
    text.includes("tests_run")
  );
}

function isObserverOrchestrationEvent(event: TaskTimelineEvent): boolean {
  const actor = (event.actor || "").toLowerCase();
  const payload = asRecord(event.payload);
  const text = eventSearchText(event);
  return (
    actor.includes("observer") &&
    !eventHasConcreteArtifacts(event) &&
    (Boolean(payload.orchestration) || text.includes("dispatch") || text.includes("planning") || text.includes("supervision"))
  );
}

function isCoarseOrInferredEvent(event: TaskTimelineEvent): boolean {
  const payload = asRecord(event.payload);
  const verification = asRecord(event.verification);
  const text = eventSearchText(event);
  return Boolean(payload.inferred || payload.coarse || verification.inferred || verification.coarse || text.includes("inferred") || text.includes("coarse"));
}

function eventHasConcreteArtifacts(event: TaskTimelineEvent): boolean {
  const artifacts = timelineEventArtifacts(event);
  return Object.values(artifacts).some((values) => values.length > 0);
}

function timelineEventArtifacts(event: TaskTimelineEvent): EventArtifactSummary {
  const payload = asRecord(event.payload);
  const verification = asRecord(event.verification);
  const artifactRefs = asRecord(event.artifact_refs);
  const containers = [payload, verification, artifactRefs];
  const changedFiles = collectStringFields(containers, ["changed_files", "files", "target_files", "modified_files", "updated_files"]);
  const tests = collectStringFields(containers, ["tests_run", "tests_written", "test_files", "test_commands", "commands", "command"]);
  const docs = stableUnique([
    ...collectStringFields(containers, ["docs_updated", "docs", "required_docs", "config_files"]),
    ...changedFiles.filter(isDocConfigOrTestFile),
  ]);
  return {
    graphTraceIds: collectStringFields(containers, ["graph_trace_ids", "graph_query_trace_ids", "query_trace_ids", "trace_ids"]),
    nodeIds: collectStringFields(containers, ["node_id", "node_ids", "target_node_id", "target_node_ids", "inspected_node_ids"]),
    nodeTitles: collectStringFields(containers, ["node_title", "node_titles", "target_node_title", "target_node_titles", "inspected_node_titles"]),
    changedFiles,
    tests,
    docs,
    screenshots: collectStringFields(containers, ["screenshot", "screenshots", "browser_screenshot", "browser_screenshots"]),
    prechecks: collectStringFields(containers, ["precheck", "prechecks", "precheck_results", "gate", "gate_result", "gate_results", "checks"]),
  };
}

function emptyArtifactSummary(): EventArtifactSummary {
  return {
    graphTraceIds: [],
    nodeIds: [],
    nodeTitles: [],
    changedFiles: [],
    tests: [],
    docs: [],
    screenshots: [],
    prechecks: [],
  };
}

function mergeArtifactSummaries(summaries: EventArtifactSummary[]): EventArtifactSummary {
  const merged = emptyArtifactSummary();
  for (const summary of summaries) {
    merged.graphTraceIds.push(...summary.graphTraceIds);
    merged.nodeIds.push(...summary.nodeIds);
    merged.nodeTitles.push(...summary.nodeTitles);
    merged.changedFiles.push(...summary.changedFiles);
    merged.tests.push(...summary.tests);
    merged.docs.push(...summary.docs);
    merged.screenshots.push(...summary.screenshots);
    merged.prechecks.push(...summary.prechecks);
  }
  return {
    graphTraceIds: stableUnique(merged.graphTraceIds),
    nodeIds: stableUnique(merged.nodeIds),
    nodeTitles: stableUnique(merged.nodeTitles),
    changedFiles: stableUnique(merged.changedFiles),
    tests: stableUnique(merged.tests),
    docs: stableUnique(merged.docs),
    screenshots: stableUnique(merged.screenshots),
    prechecks: stableUnique(merged.prechecks),
  };
}

function collectStringFields(containers: Record<string, unknown>[], keys: string[]): string[] {
  const values: string[] = [];
  for (const container of containers) {
    for (const key of keys) {
      values.push(...stringsFromUnknown(container[key]));
    }
  }
  return stableUnique(values.map((value) => value.trim()).filter(Boolean));
}

function stringsFromUnknown(value: unknown): string[] {
  if (value == null || value === "") return [];
  if (Array.isArray(value)) return value.flatMap(stringsFromUnknown);
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const preferred = ["id", "node_id", "title", "path", "file", "command", "status", "result", "trace_id", "url"];
    const direct = preferred.flatMap((key) => stringsFromUnknown(record[key]));
    if (direct.length > 0) return direct;
    return Object.entries(record).slice(0, 4).map(([key, item]) => `${key}: ${compactUnknown(item)}`);
  }
  return [String(value)];
}

function isDocConfigOrTestFile(file: string): boolean {
  return /(^|\/)(docs?|agent\/tests|frontend\/dashboard\/scripts)\//.test(file) || /\.(md|mdx|json|ya?ml|toml|ini|cfg|config|mjs)$/.test(file);
}

function eventSearchText(event: TaskTimelineEvent): string {
  return [
    event.event_type,
    event.event_kind,
    event.phase,
    event.actor,
    event.status,
    event.decision,
    JSON.stringify(event.payload ?? {}),
    JSON.stringify(event.verification ?? {}),
    JSON.stringify(event.artifact_refs ?? {}),
  ].join(" ").toLowerCase();
}

function laneLabelForEvent(event: TaskTimelineEvent): string {
  const raw = rawLaneKeyForEvent(event);
  const normalized = raw.toLowerCase();
  if (normalized.includes("front")) return "frontend";
  if (normalized.includes("back")) return "backend";
  if (normalized.includes("subagent") || normalized.includes("worker") || normalized.includes("mf_sub") || event.event_kind === "implementation") return "worker";
  if (normalized.includes("observer")) return "observer";
  if (
    normalized.includes("gate") ||
    normalized.includes("close_ready") ||
    normalized.includes("merge") ||
    normalized.includes("verify") ||
    normalized.includes("test") ||
    normalized.includes("browser") ||
    normalized.includes("playwright") ||
    normalized.includes("screenshot")
  ) {
    return "observer";
  }
  return isWorkerTimelineEvent(event) ? "worker" : "observer";
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
  if (value === "worker") return "Subagents / Workers";
  if (value === "browser_audit") return "Browser Audit";
  if (value === "gate") return "Gate";
  if (value === "merge") return "Merge";
  if (value === "retry") return "Retry";
  if (value === "verification") return "Verification";
  return value.replace(/_/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function cssToken(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "event";
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

function readBacklogIdFromUrl(): string | null {
  if (typeof window === "undefined") return null;
  const value = new URLSearchParams(window.location.search).get(BACKLOG_URL_PARAM);
  return value?.trim() || null;
}

function writeBacklogIdToUrl(backlogId: string | null, mode: "push" | "replace"): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.searchParams.set("view", "backlog");
  if (backlogId) url.searchParams.set(BACKLOG_URL_PARAM, backlogId);
  else url.searchParams.delete(BACKLOG_URL_PARAM);
  const nextUrl = `${url.pathname}${url.search}${url.hash}`;
  const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (nextUrl === currentUrl) return;
  if (mode === "push") window.history.pushState({ backlogId }, "", nextUrl);
  else window.history.replaceState({ backlogId }, "", nextUrl);
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
