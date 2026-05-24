import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  projectId as DEFAULT_PROJECT_ID,
  setProjectId as setApiProjectId,
} from "./lib/api";
import { mergeProjection } from "./lib/semantic";
import { computeNodeHealth } from "./lib/health";
import { useEventStream } from "./lib/sse";
import type {
  ActiveSummaryResponse,
  AssetImpactReminderEventsResponse,
  AssetImpactRemindersResponse,
  AssetImpactResolutionKind,
  AssetInboxResponse,
  BacklogResponse,
  EdgeRecord,
  FeedbackQueueResponse,
  HealthResponse,
  NodeRecord,
  OperationsQueueResponse,
  ProjectionResponse,
  StatusResponse,
} from "./types";
import Header from "./components/Header";
import StaleGraphBanner from "./components/StaleGraphBanner";
import TreePanel from "./components/TreePanel";
import InspectorDrawer, { type Tab as InspectorTabName } from "./components/InspectorDrawer";
import type { PinnedEdge } from "./components/FocusCard";
import ActionControlPanel, { type ActionKind, type ActionTarget, type EnrichPreset } from "./components/ActionControlPanel";
import ActionPanel from "./components/ActionPanel";
import type { BacklogDraft } from "./lib/api";
import type { AiConfigResponse, ProjectListItem } from "./lib/api";
import OverviewView from "./views/OverviewView";
import OperationsQueueView from "./views/OperationsQueueView";
import ReviewQueueView from "./views/ReviewQueueView";
import GraphView from "./views/GraphView";
import BacklogView from "./views/BacklogView";
import AssetInboxView from "./views/AssetInboxView";
import ProjectConsoleView from "./views/ProjectConsoleView";

export type ViewName = "projects" | "overview" | "graph" | "operations" | "review" | "assets" | "backlog";

const DASHBOARD_PROJECT_STORAGE_KEY = "aming-claw.dashboard.projectId";
const DASHBOARD_SIDEBAR_COLLAPSED_STORAGE_KEY = "aming-claw.dashboard.sidebarCollapsed";
const DASHBOARD_PROJECT_ID_PARAM = "project_id";
const DASHBOARD_LEGACY_PROJECT_PARAM = "project";
const DASHBOARD_VIEW_PARAM = "view";
const DASHBOARD_WORKSPACE_PARAM = "workspace";
const DASHBOARD_VIEWS: readonly ViewName[] = ["projects", "overview", "graph", "operations", "review", "assets", "backlog"];

function normalizeProjectId(value: string | null | undefined): string {
  return (value ?? "").trim() || DEFAULT_PROJECT_ID;
}

function normalizeViewName(value: string | null | undefined): ViewName {
  return DASHBOARD_VIEWS.includes(value as ViewName) ? (value as ViewName) : "projects";
}

function readStoredProjectId(): string {
  if (typeof window === "undefined") return DEFAULT_PROJECT_ID;
  try {
    return normalizeProjectId(window.localStorage.getItem(DASHBOARD_PROJECT_STORAGE_KEY));
  } catch {
    return DEFAULT_PROJECT_ID;
  }
}

function readDashboardLocation(): { projectId: string; view: ViewName; hasProjectParam: boolean; workspacePath: string } {
  if (typeof window === "undefined") {
    return { projectId: DEFAULT_PROJECT_ID, view: "projects", hasProjectParam: false, workspacePath: "" };
  }
  const params = new URLSearchParams(window.location.search);
  const projectIdParam = params.get(DASHBOARD_PROJECT_ID_PARAM);
  const legacyProjectParam = params.get(DASHBOARD_LEGACY_PROJECT_PARAM);
  const projectParam = projectIdParam?.trim() ? projectIdParam : legacyProjectParam;
  return {
    projectId: normalizeProjectId(projectParam || readStoredProjectId()),
    view: normalizeViewName(params.get(DASHBOARD_VIEW_PARAM)),
    hasProjectParam: Boolean(projectParam && projectParam.trim()),
    workspacePath: (params.get(DASHBOARD_WORKSPACE_PARAM) || "").trim(),
  };
}

function writeStoredProjectId(projectId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(DASHBOARD_PROJECT_STORAGE_KEY, normalizeProjectId(projectId));
  } catch {
    // localStorage may be disabled; URL state still preserves navigation.
  }
}

function readStoredFlag(key: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function writeStoredFlag(key: string, value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, value ? "1" : "0");
  } catch {
    // Non-critical UI preference.
  }
}

function writeDashboardLocation(projectId: string, view: ViewName, mode: "push" | "replace"): void {
  if (typeof window === "undefined") return;
  const nextProjectId = normalizeProjectId(projectId);
  const url = new URL(window.location.href);
  url.searchParams.set(DASHBOARD_PROJECT_ID_PARAM, nextProjectId);
  url.searchParams.delete(DASHBOARD_LEGACY_PROJECT_PARAM);
  url.searchParams.set(DASHBOARD_VIEW_PARAM, view);
  const nextUrl = `${url.pathname}${url.search}${url.hash}`;
  const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (nextUrl === currentUrl) return;
  const state = { projectId: nextProjectId, view };
  if (mode === "push") window.history.pushState(state, "", nextUrl);
  else window.history.replaceState(state, "", nextUrl);
}

interface DataBundle {
  health: HealthResponse;
  status: StatusResponse;
  summary: ActiveSummaryResponse;
  projection: ProjectionResponse;
  nodes: NodeRecord[];
  edges: EdgeRecord[];
  ops: OperationsQueueResponse;
  feedback: FeedbackQueueResponse;
  assetImpactReminders: AssetImpactRemindersResponse;
  assetInbox: AssetInboxResponse;
  backlog: BacklogResponse;
  loadedAt: string;
}

interface Toast {
  kind: "info" | "error" | "success";
  msg: string;
}

function shouldFallbackToProjects(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  return error.status === 400 || error.status === 404;
}

const CLOSED_BACKLOG_STATUSES = new Set([
  "FIXED",
  "CLOSED",
  "DONE",
  "RESOLVED",
  "CANCELLED",
  "MERGED",
  "SUPERSEDED",
  "VOID",
]);

function emptyOperationsQueue(projectId: string, snapshotId: string): OperationsQueueResponse {
  return {
    ok: true,
    project_id: projectId,
    snapshot_id: snapshotId,
    active_snapshot_id: snapshotId,
    count: 0,
    operations: [],
    summary: {
      by_type: {},
      by_status: {},
      pending_scope_reconcile_count: 0,
    },
  };
}

function emptyAssetImpactReminders(
  projectId: string,
  opts: { unavailable?: boolean; error?: string } = {},
): AssetImpactRemindersResponse {
  return {
    ok: opts.unavailable ? false : true,
    project_id: projectId,
    status: "pending",
    asset_kind: "",
    reminders: [],
    count: 0,
    summary: { total: 0, pending: 0, by_kind: {}, by_asset_kind: {}, by_status: {} },
    unavailable: opts.unavailable,
    error: opts.error,
  };
}

function countAssetImpactReminders(response: AssetImpactRemindersResponse | null | undefined): number {
  return (response?.reminders ?? response?.items ?? []).length;
}

const DEFAULT_AI_MODELS: Record<string, string[]> = {
  anthropic: ["claude-opus-4-7", "claude-sonnet-4-6", "claude-sonnet-4-5"],
  openai: ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex"],
};

export default function App() {
  const [data, setData] = useState<DataBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const loadingRef = useRef(loading);
  const [error, setError] = useState<string | null>(null);
  const initialLocation = useMemo(() => readDashboardLocation(), []);
  const [view, setView] = useState<ViewName>(initialLocation.view);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [pinnedEdge, setPinnedEdge] = useState<PinnedEdge | null>(null);
  const [drawerTab, setDrawerTab] = useState<InspectorTabName>("overview");
  const [actionPanel, setActionPanel] = useState<{ kind: ActionKind; target: ActionTarget } | null>(null);
  const [actionPanelOpen, setActionPanelOpen] = useState(false);
  const [actionPanelInitialTab, setActionPanelInitialTab] = useState<"review" | "backlog">("review");
  const [actionPanelPrefill, setActionPanelPrefill] = useState<Partial<BacklogDraft> | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const [reconcileBusy, setReconcileBusy] = useState(false);
  // MF-016 banner P3: surface reconcile progress inline. Each phase covers a
  // discrete step the user can read off — no more "did the click do anything?"
  const [reconcilePhase, setReconcilePhase] = useState<
    "idle" | "queueing" | "materializing" | "rebuilding" | "done" | "error"
  >("idle");
  const [reconcileDetail, setReconcileDetail] = useState<string>("");
  // Multi-select mode: operator toggles it on to batch-enrich many targets at
  // once. Graph clicks switch from "pin / select" to "add to bucket". IDs are
  // prefixed `node:<id>` / `edge:<id>` so the single Set can hold both.
  const [multiSelectMode, setMultiSelectMode] = useState(false);
  const [multiSelectIds, setMultiSelectIds] = useState<Set<string>>(() => new Set());
  const multiSelectIdsRef = useRef<Set<string>>(new Set());
  const [batchEnrichBusy, setBatchEnrichBusy] = useState(false);
  const [currentProjectId, setCurrentProjectId] = useState(initialLocation.projectId);
  const currentProjectIdRef = useRef(currentProjectId);
  const [projects, setProjects] = useState<ProjectListItem[]>([]);
  const [bootstrapWorkspacePath, setBootstrapWorkspacePath] = useState(initialLocation.workspacePath);
  const [aiConfig, setAiConfig] = useState<AiConfigResponse | null>(null);
  const [aiConfigOpen, setAiConfigOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() =>
    readStoredFlag(DASHBOARD_SIDEBAR_COLLAPSED_STORAGE_KEY),
  );
  const [assetImpactEventsByReminder, setAssetImpactEventsByReminder] = useState<
    Record<string, AssetImpactReminderEventsResponse>
  >({});
  const [assetImpactBusyId, setAssetImpactBusyId] = useState<string | null>(null);
  const [assetImpactError, setAssetImpactError] = useState<string | null>(null);

  useEffect(() => {
    multiSelectIdsRef.current = multiSelectIds;
  }, [multiSelectIds]);

  useEffect(() => {
    setApiProjectId(currentProjectId);
    currentProjectIdRef.current = currentProjectId;
    writeStoredProjectId(currentProjectId);
    writeDashboardLocation(currentProjectId, view, "replace");
  }, [currentProjectId, view]);

  useEffect(() => {
    writeStoredFlag(DASHBOARD_SIDEBAR_COLLAPSED_STORAGE_KEY, sidebarCollapsed);
  }, [sidebarCollapsed]);

  const resetProjectScopedUi = useCallback(() => {
    setData(null);
    setError(null);
    setSelectedNodeId(null);
    setPinnedEdge(null);
    setActionPanel(null);
    setActionPanelOpen(false);
    setAiConfig(null);
    setMultiSelectIds(new Set());
    multiSelectIdsRef.current = new Set();
    setAssetImpactEventsByReminder({});
    setAssetImpactBusyId(null);
    setAssetImpactError(null);
  }, []);

  useEffect(() => {
    const handlePopState = () => {
      const next = readDashboardLocation();
      setApiProjectId(next.projectId);
      if (currentProjectIdRef.current !== next.projectId) resetProjectScopedUi();
      setCurrentProjectId(next.projectId);
      setView(next.view);
      setBootstrapWorkspacePath(next.workspacePath);
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [resetProjectScopedUi]);

  const fetchAll = useCallback(async (signal?: AbortSignal) => {
    const requestProjectId = currentProjectId;
    setApiProjectId(requestProjectId);
    setLoading(true);
    setError(null);
    try {
      const [health, projectList] = await Promise.all([
        api.health(signal),
        api.projects(signal),
      ]);
      setProjects(projectList.projects ?? []);
      const projectKnown = (projectList.projects ?? []).some((project) => project.project_id === requestProjectId);
      if (!projectKnown && view === "projects") {
        setData(null);
        setAiConfig(null);
        return;
      }
      if (!projectKnown && view !== "projects") {
        setData(null);
        setAiConfig(null);
        setView("projects");
        setToast({
          kind: "info",
          msg: `Project ${requestProjectId} is not registered yet. Use Projects to bootstrap it.`,
        });
        return;
      }
      const [status, summary, projection, backlog, aiCfg] = await Promise.all([
        api.statusFor(requestProjectId, signal),
        api.activeSummaryFor(requestProjectId, signal),
        api.activeProjectionFor(requestProjectId, signal),
        api.backlogFor(requestProjectId, signal),
        api.aiConfigFor(requestProjectId, signal),
      ]);
      setAiConfig(aiCfg);
      const snapshotId = status.active_snapshot_id || summary.snapshot_id;
      const [nodesRes, edgesRes, feedback, assetInbox, assetImpactReminders] = await Promise.all([
        api.nodesFor(requestProjectId, snapshotId, 1000, signal),
        api.edgesFor(requestProjectId, snapshotId, 4000, signal),
        api.feedbackQueueFor(requestProjectId, snapshotId, signal),
        api.assetInboxFor(requestProjectId, snapshotId, signal),
        api
          .assetImpactRemindersFor(requestProjectId, { asset_kind: "", status: "pending" }, signal)
          .catch((assetImpactError) => {
            if ((assetImpactError as { name?: string }).name === "AbortError") throw assetImpactError;
            const msg =
              assetImpactError instanceof ApiError
                ? `${assetImpactError.message} ${assetImpactError.body}`
                : (assetImpactError as Error).message;
            console.warn("Asset impact reminders refresh failed", assetImpactError);
            return emptyAssetImpactReminders(requestProjectId, { unavailable: true, error: msg });
          }),
      ]);
      // projection.projection is null when the snapshot was just rebuilt and
      // the semantic projection hasn't been computed yet. mergeProjection
      // tolerates an empty map.
      const merged = mergeProjection(nodesRes.nodes, projection?.projection?.node_semantics ?? {});
      // Per-node feature health (prototype algorithm — leafScore for L7 leaves,
      // recursive average for containers; L4 leaves are intentionally unscored).
      const healthMap = computeNodeHealth(merged, edgesRes.edges);
      const mergedWithHealth = merged.map((n) => {
        const h = healthMap.get(n.node_id);
        return h ? { ...n, _health: h._health } : n;
      });
      setData({
        health,
        status,
        summary,
        projection,
        nodes: mergedWithHealth,
        edges: edgesRes.edges,
        ops: emptyOperationsQueue(requestProjectId, snapshotId),
        feedback,
        assetImpactReminders,
        assetInbox,
        backlog,
        loadedAt: new Date().toISOString(),
      });
      api.operationsQueueFor(requestProjectId, signal)
        .then((ops) => {
          setData((current) => {
            if (!current) return current;
            const currentProjectId = current.status.project_id || current.summary.project_id;
            const currentSnapshotId = current.status.active_snapshot_id || current.summary.snapshot_id;
            if (currentProjectId !== requestProjectId || currentSnapshotId !== snapshotId) return current;
            return { ...current, ops };
          });
        })
        .catch((opsError) => {
          if ((opsError as { name?: string }).name === "AbortError") return;
          console.warn("Operations queue refresh failed", opsError);
        });
    } catch (e) {
      if ((e as { name?: string }).name === "AbortError") return;
      if (shouldFallbackToProjects(e) && view === "projects") {
        setData(null);
        setAiConfig(null);
        setError(null);
        return;
      }
      if (shouldFallbackToProjects(e) && view !== "projects") {
        setData(null);
        setAiConfig(null);
        setView("projects");
        const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
        setToast({
          kind: "info",
          msg: `Graph is not ready for ${requestProjectId}. Open Projects to bootstrap or build graph.`,
        });
        setError(null);
        console.info("Falling back to Projects view:", msg);
        return;
      }
      const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
      setError(msg);
      setToast({ kind: "error", msg: `Load failed: ${msg}` });
    } finally {
      setLoading(false);
    }
  }, [currentProjectId, view]);

  useEffect(() => {
    const ac = new AbortController();
    fetchAll(ac.signal);
    return () => ac.abort();
  }, [fetchAll]);

  // Live sync: SSE pushes a 'dashboard.changed' event whenever any mutating
  // graph-governance endpoint succeeds (server.py:_emit_dashboard_changed),
  // plus pass-through for node.status_changed etc. We debounce a refetch so
  // bursts (e.g. worker draining 20 nodes in 1s) collapse into one call.
  const refetchTimerRef = useRef<number | null>(null);
  const liveRefetchPendingRef = useRef(false);
  const scheduleLiveRefetch = useCallback(() => {
    if (refetchTimerRef.current != null) window.clearTimeout(refetchTimerRef.current);
    refetchTimerRef.current = window.setTimeout(() => {
      refetchTimerRef.current = null;
      // If a completion event lands during a load, keep a pending refresh
      // marker. The in-flight load often started before the worker committed
      // its final state, so dropping the event can leave rows stuck as running.
      if (loadingRef.current) {
        liveRefetchPendingRef.current = true;
        return;
      }
      liveRefetchPendingRef.current = false;
      void fetchAll();
    }, 600);
  }, [fetchAll]);

  useEffect(() => {
    loadingRef.current = loading;
    if (!loading && liveRefetchPendingRef.current) {
      scheduleLiveRefetch();
    }
  }, [loading, scheduleLiveRefetch]);

  useEffect(
    () => () => {
      if (refetchTimerRef.current != null) window.clearTimeout(refetchTimerRef.current);
    },
    [],
  );

  const liveStatus = useEventStream(currentProjectId, {
    enabled: true,
    onEvent: scheduleLiveRefetch,
  });

  const handleQueueReconcile = useCallback(async () => {
    if (reconcileBusy) return;
    const headCommit = data?.status?.current_state?.graph_stale?.head_commit;
    const snapCommit = data?.status?.graph_snapshot_commit;
    if (!headCommit) {
      setToast({ kind: "error", msg: "Cannot reconcile: HEAD commit unknown (status response missing)." });
      return;
    }
    const ok = window.confirm(
      `Catch the active graph up to HEAD ${headCommit.slice(0, 7)}? Runs the ` +
        "scope reconcile inline (materialize+activate → projection rebuild). " +
        "The banner shows live progress.",
    );
    if (!ok) return;
    setReconcileBusy(true);
    // Skip the "queueing" phase chip — the queue API call is ~100ms and the
    // visible step just flashed by. Start at "materializing" which covers
    // the queue + build round-trip from the operator's POV.
    setReconcilePhase("materializing");
    setReconcileDetail(`target ${headCommit.slice(0, 7)}`);
    try {
      // Direct materialize + activate in one round-trip. The backend creates
      // and consumes any transient pending-scope bookkeeping internally, so the
      // operator does not see a separate queued scope-reconcile task.
      setReconcilePhase("materializing");
      setReconcileDetail(`building snapshot for ${headCommit.slice(0, 7)}`);
      const runRes = await api.materializeAndActivatePendingScope({
        target_commit_sha: headCommit,
        parent_commit_sha: snapCommit ?? undefined,
        semantic_use_ai: false, // rule-based + carry-forward only; no AI billed
        actor: "dashboard_user",
      });
      const newSid = runRes.snapshot_id || runRes.activation?.snapshot_id || "(unknown)";
      const projection = runRes.activation?.projection_status ?? "(unknown)";
      setReconcilePhase("rebuilding");
      setReconcileDetail(`activated ${newSid.slice(0, 20)} · projection ${projection}`);
      setToast({
        kind: "success",
        msg:
          `Scope reconcile complete · snapshot=${newSid.slice(0, 24)} · ` +
          `projection=${projection}. Refreshing dashboard…`,
      });
      await fetchAll();
      setReconcilePhase("done");
      setReconcileDetail(`active snapshot is now ${newSid.slice(0, 20)}`);
      // Drop the banner status after a short visible window so the operator can
      // confirm it succeeded; the banner itself will disappear once the stale
      // condition clears in the refreshed status.
      window.setTimeout(() => {
        setReconcilePhase("idle");
        setReconcileDetail("");
      }, 4000);
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
      setReconcilePhase("error");
      setReconcileDetail(msg.slice(0, 120));
      setToast({ kind: "error", msg: `Reconcile failed: ${msg}` });
    } finally {
      setReconcileBusy(false);
    }
  }, [reconcileBusy, data, fetchAll]);

  const handleClearTerminal = useCallback(async () => {
    const snapshotId = data?.status?.active_snapshot_id;
    if (!snapshotId) {
      setToast({ kind: "error", msg: "No active snapshot." });
      return;
    }
    const ok = window.confirm(
      "Permanently delete all cancelled / complete / failed node queue rows from this snapshot? Edge audit events are preserved.",
    );
    if (!ok) return;
    try {
      const res = await api.clearTerminalSemanticJobs(snapshotId, {});
      setToast({
        kind: res.ok ? "success" : "error",
        msg: `Clear terminal · deleted_nodes=${res.deleted_count} · edge_audit_matched=${res.edge_audit_matched}`,
      });
      fetchAll();
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
      setToast({ kind: "error", msg: `Clear terminal failed: ${msg}` });
    }
  }, [data, fetchAll]);

  const handleCancelAllByType = useCallback(
    async (opType: "node_semantic" | "edge_semantic") => {
      const snapshotId = data?.status?.active_snapshot_id;
      if (!snapshotId) {
        setToast({ kind: "error", msg: "No active snapshot." });
        return;
      }
      const ok = window.confirm(
        `Cancel ALL queued ${opType} jobs in this snapshot? Terminal rows are not affected.`,
      );
      if (!ok) return;
      try {
        const res = await api.cancelAllSemanticJobs(snapshotId, {
          operation_type: opType,
          status: "queued",
        });
        setToast({
          kind: res.ok ? "success" : "error",
          msg: `Cancel-all ${opType} · cancelled=${res.cancelled_count} · skipped_terminal=${res.skipped_terminal} · matched=${res.matched_count ?? "?"}`,
        });
        fetchAll();
      } catch (e) {
        const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
        setToast({ kind: "error", msg: `Cancel-all failed: ${msg}` });
      }
    },
    [data, fetchAll],
  );

  const handleFeedbackDecision = useCallback(
    async (feedbackIds: string[], action: string, summaryHint?: string) => {
      const snapshotId = data?.status?.active_snapshot_id;
      if (!snapshotId) {
        setToast({ kind: "error", msg: "No active snapshot." });
        return;
      }
      if (feedbackIds.length === 0) {
        setToast({ kind: "error", msg: "No feedback ids selected." });
        return;
      }
      const idsLabel = feedbackIds.length === 1 ? feedbackIds[0] : `${feedbackIds.length} items`;
      const ok = window.confirm(
        `${action} for ${idsLabel}${summaryHint ? ` (${summaryHint})` : ""}?`,
      );
      if (!ok) return;
      try {
        const res = await api.decideFeedback(snapshotId, {
          feedback_ids: feedbackIds,
          action,
        });
        const accepted = res.semantic_enrichment_accepted;
        const flipped = accepted?.node_ids_flipped?.length ?? 0;
        const proj = res.projection_rebuilt ? "projection rebuilt" : "projection unchanged";
        setToast({
          kind: res.ok === false ? "error" : "success",
          msg:
            `${action} · decided=${res.decided_count ?? 0} · errors=${res.error_count ?? 0}` +
            (action === "accept_semantic_enrichment"
              ? ` · nodes flipped=${flipped} · ${proj}`
              : ""),
        });
        fetchAll();
      } catch (e) {
        const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
        setToast({ kind: "error", msg: `${action} failed: ${msg}` });
      }
    },
    [data, fetchAll],
  );

  const handleFeedbackRetry = useCallback(
    async (feedbackIds: string[], nodeId: string, rationale: string) => {
      const snapshotId = data?.status?.active_snapshot_id;
      if (!snapshotId) {
        setToast({ kind: "error", msg: "No active snapshot." });
        return;
      }
      const reason = rationale.trim();
      if (!reason) {
        setToast({ kind: "error", msg: "Retry needs a rationale." });
        return;
      }
      try {
        // Step 1: close the current feedback row as rejected with the operator
        // rationale so the Review Queue stops showing the stale proposal.
        await api.decideFeedback(snapshotId, {
          feedback_ids: feedbackIds,
          action: "reject_false_positive",
          rationale: reason,
        });
        // Step 2: append the rationale to the JSONL semantic-feedback store
        // that run_semantic_enrichment reads — this is what the next AI run
        // sees in its `review_feedback` array alongside `existing_semantic`.
        await api.appendSemanticFeedback(
          snapshotId,
          [
            {
              target_type: "node",
              target_id: nodeId,
              issue: reason,
              priority: "P2",
              source_node_ids: [nodeId],
              reason,
            },
          ],
          "dashboard_user",
        );
        // Step 3: re-enqueue the node. Worker picks it up via EventBus and the
        // AI call now has the prior rejected semantic + new rationale.
        await api.submitSemanticJob(snapshotId, {
          job_type: "semantic_enrichment",
          target_scope: "node",
          target_ids: [nodeId],
          options: { mode: "retry", target: "nodes" },
          created_by: "dashboard_user",
        });
        setToast({
          kind: "success",
          msg: `Retry queued for ${nodeId} · prior proposal rejected · rationale forwarded to next AI run`,
        });
        fetchAll();
      } catch (e) {
        const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
        setToast({ kind: "error", msg: `Retry failed: ${msg}` });
      }
    },
    [data, fetchAll],
  );

  const handleLoadAssetImpactEvents = useCallback(async (reminderId: string) => {
    const id = reminderId.trim();
    if (!id) return;
    setAssetImpactBusyId(`events:${id}`);
    setAssetImpactError(null);
    try {
      const res = await api.assetImpactReminderEventsFor(currentProjectIdRef.current, id);
      setAssetImpactEventsByReminder((current) => ({ ...current, [id]: res }));
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
      setAssetImpactError(msg);
      setToast({ kind: "error", msg: `Asset impact events failed: ${msg}` });
    } finally {
      setAssetImpactBusyId(null);
    }
  }, []);

  const handleResolveAssetImpactReminder = useCallback(
    async (reminderId: string, resolutionKind: AssetImpactResolutionKind, note: string) => {
      const id = reminderId.trim();
      if (!id) {
        setToast({ kind: "error", msg: "No asset impact reminder selected." });
        return;
      }
      const label = resolutionKind === "keep_unchanged" ? "keep unchanged" : resolutionKind;
      const ok = window.confirm(`Resolve asset impact reminder ${id} as ${label}?`);
      if (!ok) return;
      setAssetImpactBusyId(`resolve:${id}`);
      setAssetImpactError(null);
      try {
        const res = await api.resolveAssetImpactReminderFor(currentProjectIdRef.current, id, {
          resolution_kind: resolutionKind,
          note: note.trim(),
          actor: "dashboard_user",
        });
        setToast({
          kind: res.ok === false ? "error" : "success",
          msg: `Asset impact ${label} · covers=${
            (res.resolution?.covers_event_ids ?? res.covers_event_ids ?? []).length
          }`,
        });
        setAssetImpactEventsByReminder((current) => {
          const next = { ...current };
          delete next[id];
          return next;
        });
        fetchAll();
      } catch (e) {
        const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
        setAssetImpactError(msg);
        setToast({ kind: "error", msg: `Asset impact resolve failed: ${msg}` });
      } finally {
        setAssetImpactBusyId(null);
      }
    },
    [fetchAll],
  );

  // Multi-select handlers
  const toggleMultiSelect = useCallback((kind: "node" | "edge", id: string) => {
    const next = new Set(multiSelectIdsRef.current);
    const key = `${kind}:${id}`;
    if (next.has(key)) next.delete(key);
    else next.add(key);
    multiSelectIdsRef.current = next;
    setMultiSelectIds(next);
  }, []);

  const clearMultiSelect = useCallback(() => {
    const next = new Set<string>();
    multiSelectIdsRef.current = next;
    setMultiSelectIds(next);
  }, []);

  const handleBatchEnrich = useCallback(async () => {
    const snapshotId = data?.status?.active_snapshot_id;
    if (!snapshotId) {
      setToast({ kind: "error", msg: "No active snapshot." });
      return;
    }
    const readiness = semanticAiReadiness(aiConfig);
    if (!readiness.ready) {
      setToast({ kind: "error", msg: readiness.blockMessage });
      return;
    }
    const selectedKeys = Array.from(multiSelectIdsRef.current);
    if (selectedKeys.length === 0) {
      setToast({ kind: "error", msg: "Pick at least one node or edge first." });
      return;
    }
    const nodeIds: string[] = [];
    const edgeIds: string[] = [];
    selectedKeys.forEach((k) => {
      const idx = k.indexOf(":");
      const kind = k.slice(0, idx);
      const id = k.slice(idx + 1);
      if (kind === "node") nodeIds.push(id);
      else if (kind === "edge") edgeIds.push(id);
    });
    const ok = window.confirm(
      `Queue AI enrich for ${nodeIds.length} node(s) and ${edgeIds.length} edge(s)?`,
    );
    if (!ok) return;
    setBatchEnrichBusy(true);
    try {
      const summary: string[] = [];
      const partial: string[] = [];
      if (nodeIds.length > 0) {
        const res = await api.submitSemanticJob(snapshotId, {
          job_type: "semantic_enrichment",
          target_scope: "node",
          target_ids: nodeIds,
          options: {
            target: "nodes",
            scope: "selected_nodes",
            mode: "retry",
            dry_run: false,
            include_nodes: true,
            include_edges: false,
            skip_current: false,
            retry_stale_failed: true,
            include_package_markers: false,
          },
          created_by: "dashboard_user",
        });
        const queued = res.queued_count ?? nodeIds.length;
        summary.push(`nodes ${queued}/${nodeIds.length}`);
        if (queued < nodeIds.length) partial.push(`nodes queued ${queued}/${nodeIds.length}`);
      }
      if (edgeIds.length > 0) {
        const res = await api.submitSemanticJob(snapshotId, {
          job_type: "semantic_enrichment",
          target_scope: "edge",
          target_ids: edgeIds,
          options: {
            target: "edges",
            scope: "selected_edges",
            mode: "semanticize",
            dry_run: false,
            include_nodes: false,
            include_edges: true,
          },
          created_by: "dashboard_user",
        });
        const queued = res.queued_count ?? edgeIds.length;
        summary.push(`edges ${queued}/${edgeIds.length}`);
        if (queued < edgeIds.length) partial.push(`edges queued ${queued}/${edgeIds.length}`);
      }
      setToast({
        kind: partial.length > 0 ? "info" : "success",
        msg: `Batch AI enrich queued · ${summary.join(" · ")}${partial.length > 0 ? " · check queue for skipped/current targets" : ""}`,
      });
      const next = new Set<string>();
      multiSelectIdsRef.current = next;
      setMultiSelectIds(next);
      // Auto-exit multi-select mode so subsequent clicks behave normally.
      setMultiSelectMode(false);
      fetchAll();
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
      setToast({ kind: "error", msg: `Batch enrich failed: ${msg}` });
    } finally {
      setBatchEnrichBusy(false);
    }
  }, [aiConfig, data, fetchAll]);

  const handleSelectNodeFromReview = useCallback(
    (id: string) => {
      setSelectedNodeId(id);
      setPinnedEdge(null);
      setView("graph");
      setDrawerTab("overview");
    },
    [],
  );

  // MF-016 P3 follow-up: edge feedback rows use target_id of the form
  // `<src>-><dst>:<type>` (e.g. "L7.1->L4.1:creates_task"). Parse it and pin
  // the matching edge on the graph view so clicking the Review Queue chip
  // jumps the same way node clicks do.
  const handleSelectEdgeFromReview = useCallback(
    (edgeId: string) => {
      // Edge target_id ships in two formats depending on who created the
      // feedback row:
      //   1. `<src>-><dst>:<type>` — worker writes this when it enriches an
      //      edge_semantic_requested event (semantic_worker, server.py edge
      //      POST handler).
      //   2. `<src>|<dst>|<type>` — ActionControlPanel writes this when the
      //      operator files feedback from the graph view.
      // Try both, normalize to {src, dst, type}.
      let src = "", dst = "", type = "";
      if (edgeId.includes("|")) {
        const parts = edgeId.split("|");
        if (parts.length < 2) {
          setToast({ kind: "error", msg: `Cannot parse edge id ${edgeId}` });
          return;
        }
        [src, dst, type = ""] = parts;
      } else {
        const arrowIdx = edgeId.indexOf("->");
        if (arrowIdx < 0) {
          setToast({ kind: "error", msg: `Cannot parse edge id ${edgeId}` });
          return;
        }
        src = edgeId.slice(0, arrowIdx);
        const rest = edgeId.slice(arrowIdx + 2);
        const colonIdx = rest.indexOf(":");
        dst = colonIdx >= 0 ? rest.slice(0, colonIdx) : rest;
        type = colonIdx >= 0 ? rest.slice(colonIdx + 1) : "";
      }
      // Look up the real edge record so confidence/evidence/direction come
      // from the live graph instead of being blanked out on the pin.
      const real = data?.edges.find(
        (e) =>
          e.src === src && e.dst === dst && (e.type === type || e.edge_type === type),
      );
      setSelectedNodeId(null);
      setPinnedEdge({
        src,
        dst,
        type: type || real?.type || real?.edge_type || "",
        evidence: real?.evidence,
        direction: real?.direction,
        confidence: real?.confidence,
      });
      setView("graph");
      setDrawerTab("overview");
    },
    [data],
  );

  const handleCancelOperation = useCallback(
    async (opType: string, opId: string, targetId: string) => {
      const snapshotId = data?.status?.active_snapshot_id;
      try {
        if (opType === "scope_reconcile") {
          const ok = window.confirm(`Cancel scope reconcile for ${targetId.slice(0, 12)}?`);
          if (!ok) return;
          const res = await api.cancelScopeReconcile({ operation_id: opId });
          setToast({
            kind: res.status === "cancelled" ? "success" : "info",
            msg: `Reconcile cancel · ${res.status} · waived=${res.cancelled_count}`,
          });
        } else if (opType === "node_semantic" || opType === "edge_semantic" || opType === "ai_summary") {
          if (!snapshotId) throw new Error("no active snapshot");
          const ok = window.confirm(`Cancel ${opType} job for ${targetId}?`);
          if (!ok) return;
          const res = await api.cancelSemanticJob(snapshotId, targetId);
          setToast({
            kind: res.ok ? "success" : "error",
            msg: `${opType} cancel · status=${res.job?.status ?? "?"}`,
          });
        } else {
          setToast({ kind: "info", msg: `Cancel for ${opType} not wired yet.` });
          return;
        }
        fetchAll();
      } catch (e) {
        const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
        setToast({ kind: "error", msg: `Cancel failed: ${msg}` });
      }
    },
    [data, fetchAll],
  );

  const handleRefresh = useCallback(() => fetchAll(), [fetchAll]);

  const handleProjectChange = useCallback((nextProjectId: string) => {
    const next = nextProjectId.trim() || DEFAULT_PROJECT_ID;
    setApiProjectId(next);
    writeStoredProjectId(next);
    writeDashboardLocation(next, view, "push");
    setCurrentProjectId(next);
    resetProjectScopedUi();
  }, [resetProjectScopedUi, view]);

  const handleOpenProject = useCallback(
    (nextProjectId: string) => {
      const next = nextProjectId.trim() || DEFAULT_PROJECT_ID;
      setApiProjectId(next);
      writeStoredProjectId(next);
      writeDashboardLocation(next, "overview", "push");
      setCurrentProjectId(next);
      setView("overview");
      resetProjectScopedUi();
    },
    [resetProjectScopedUi],
  );

  // Auto-dismiss toasts.
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(t);
  }, [toast]);

  const selectedNode = useMemo(() => {
    if (!data || !selectedNodeId) return null;
    return data.nodes.find((n) => n.node_id === selectedNodeId) ?? null;
  }, [data, selectedNodeId]);
  const activeWorkspaceRoot = useMemo(
    () => projects.find((p) => p.project_id === currentProjectId)?.workspace_path || "",
    [currentProjectId, projects],
  );

  const handleSelectNode = useCallback((id: string) => {
    // Tree / drawer / FocusCard navigation always sets focus, even in
    // multi-select mode — operator needs to be able to position the graph
    // to pick targets from. Graph-internal clicks use handleGraphSelectNode
    // which toggles the bucket instead.
    setSelectedNodeId(id);
    setPinnedEdge(null);
    setView((prev) => (prev === "graph" ? prev : "graph"));
  }, []);

  const handleGraphSelectNode = useCallback(
    (id: string) => {
      if (multiSelectMode) {
        toggleMultiSelect("node", id);
        return;
      }
      setSelectedNodeId(id);
      setPinnedEdge(null);
    },
    [multiSelectMode, toggleMultiSelect],
  );

  const handlePinEdge = useCallback(
    (edge: PinnedEdge | null) => {
      if (multiSelectMode && edge) {
        // Encode the canonical edge id used by the worker / projection
        // (`<src>-><dst>:<type>`).
        toggleMultiSelect("edge", `${edge.src}->${edge.dst}:${edge.type}`);
        return;
      }
      setPinnedEdge(edge);
    },
    [multiSelectMode, toggleMultiSelect],
  );

  const handleOpenDrawerTab = useCallback((tab: InspectorTabName) => {
    setDrawerTab(tab);
  }, []);

  const handleOpenAction = useCallback((kind: ActionKind, target: ActionTarget) => {
    setActionPanel({ kind, target });
  }, []);

  const handleOpenPreset = useCallback((preset: EnrichPreset) => {
    setActionPanelOpen(false);
    setActionPanel({ kind: "enrich", target: { preset } });
  }, []);

  const handleOpenBacklog = useCallback((target: ActionTarget) => {
    // Pre-fill the backlog draft from the target node so the form lands ready
    // to submit. For edges we use the source node's primary files; the backend
    // will store both endpoints in affected_graph_nodes.
    let prefill: Partial<BacklogDraft> = {};
    if (target.node) {
      const n = target.node;
      const lastSeg = (n.title || n.node_id).split(".").slice(-1)[0];
      prefill = {
        title: `Follow-up on ${lastSeg}`,
        target_files: (n.primary_files ?? []).slice(0, 3) as unknown as BacklogDraft["target_files"],
        affected_graph_nodes: [n.node_id] as unknown as BacklogDraft["affected_graph_nodes"],
      };
    } else if (target.edge) {
      const e = target.edge;
      prefill = {
        title: `Follow-up on ${e.type} edge`,
        affected_graph_nodes: [e.src, e.dst] as unknown as BacklogDraft["affected_graph_nodes"],
      };
    }
    // BacklogDraft form fields hold raw textarea/csv strings during editing;
    // convert array → string for the seed.
    if (prefill.target_files) {
      prefill = { ...prefill, target_files: (prefill.target_files as unknown as string[]).join("\n") as unknown as BacklogDraft["target_files"] };
    }
    if (prefill.affected_graph_nodes) {
      prefill = {
        ...prefill,
        affected_graph_nodes: (prefill.affected_graph_nodes as unknown as string[]).join(", ") as unknown as BacklogDraft["affected_graph_nodes"],
      };
    }
    setActionPanelPrefill(prefill);
    setActionPanelInitialTab("backlog");
    setActionPanelOpen(true);
  }, []);

  return (
    <div className="app">
      <Header
        loading={loading}
        summary={data?.summary}
        status={data?.status}
        health={data?.health}
        ops={data?.ops}
        loadedAt={data?.loadedAt}
        projectId={currentProjectId}
        projects={projects}
        aiConfig={aiConfig}
        onRefresh={handleRefresh}
        onProjectChange={handleProjectChange}
        onOpenAiConfig={() => setAiConfigOpen(true)}
        liveStatus={liveStatus}
        multiSelectMode={multiSelectMode}
        multiSelectCount={multiSelectIds.size}
        batchEnrichBusy={batchEnrichBusy}
        onToggleMultiSelect={() => {
          setMultiSelectMode((prev) => !prev);
          if (multiSelectMode) {
            const next = new Set<string>();
            multiSelectIdsRef.current = next;
            setMultiSelectIds(next);
          }
        }}
        onBatchEnrich={handleBatchEnrich}
        onClearMultiSelect={clearMultiSelect}
      />
      <StaleGraphBanner
        health={data?.health}
        status={data?.status}
        busy={reconcileBusy}
        phase={reconcilePhase}
        phaseDetail={reconcileDetail}
        onQueueReconcile={handleQueueReconcile}
      />
      <div className="app-body">
        <TreePanel
          nodes={data?.nodes ?? []}
          selectedNodeId={selectedNodeId}
          activeView={view}
          opsCount={data?.ops?.count ?? 0}
          reviewCount={
            (data?.feedback?.summary?.visible_group_count ?? 0) +
            countAssetImpactReminders(data?.assetImpactReminders)
          }
          assetCount={data?.assetInbox?.summary?.operator_review_count ?? 0}
          backlogCount={countOpenBacklog(data?.backlog)}
          projectCount={projects.length}
          onSelectNode={handleSelectNode}
          onSelectView={(v) => setView(v)}
          loading={loading}
          collapsed={sidebarCollapsed}
          onToggleCollapsed={() => setSidebarCollapsed((prev) => !prev)}
        />
        <main className="main scrollbar-thin">
          {error && !data && view !== "projects" ? (
            <div className="view">
              <div className="empty">
                Load failed. Check the governance service is reachable at{" "}
                <span className="mono">/api/*</span>.<br />
                <span className="mono" style={{ color: "var(--ink-700)" }}>{error}</span>
              </div>
            </div>
          ) : null}
          {view === "projects" ? (
            <ProjectConsoleView
              projects={projects}
              currentProjectId={currentProjectId}
              initialWorkspacePath={bootstrapWorkspacePath}
              loading={loading}
              onOpenProject={handleOpenProject}
              onOpenAiConfig={() => setAiConfigOpen(true)}
              onRefresh={handleRefresh}
            />
          ) : null}
          {view === "overview" && data ? (
            <OverviewView data={data} onSelectNode={handleSelectNode} />
          ) : null}
          {view === "graph" && data ? (
            <div className="graph-with-drawer">
              <div className="graph-with-drawer-main">
                <GraphView
                  nodes={data.nodes}
                  edges={data.edges}
                  selectedNodeId={selectedNodeId}
                  pinnedEdge={pinnedEdge}
                  onPinEdge={handlePinEdge}
                  multiSelectMode={multiSelectMode}
                  multiSelectIds={multiSelectIds}
                  onSelectNode={handleGraphSelectNode}
                  onOpenDrawerTab={handleOpenDrawerTab}
                  onOpenAction={handleOpenAction}
                />
              </div>
              {pinnedEdge || selectedNode ? (
                <InspectorDrawer
                  node={selectedNode}
                  pinnedEdge={pinnedEdge}
                  allNodes={data.nodes}
                  edges={data.edges}
                  feedback={data.feedback}
                  snapshotId={data.status?.active_snapshot_id ?? data.summary?.snapshot_id ?? null}
                  edgeSemantics={
                    (data.projection?.projection?.edge_semantics as
                      | Record<string, unknown>
                      | undefined) ?? null
                  }
                  workspaceRoot={activeWorkspaceRoot}
                  onSelectNode={handleSelectNode}
                  onClose={() => {
                    setSelectedNodeId(null);
                    setPinnedEdge(null);
                  }}
                  onClearEdge={() => setPinnedEdge(null)}
                  onOpenAction={handleOpenAction}
                  onOpenBacklog={handleOpenBacklog}
                  onDecide={handleFeedbackDecision}
                  onRetry={handleFeedbackRetry}
                  tab={drawerTab}
                  onTabChange={setDrawerTab}
                />
              ) : null}
            </div>
          ) : null}
          {view === "operations" && data ? (
            <OperationsQueueView
              ops={data.ops}
              onCancelOperation={handleCancelOperation}
              onCancelAllByType={handleCancelAllByType}
              onClearTerminal={handleClearTerminal}
            />
          ) : null}
          {view === "review" && data ? (
            <ReviewQueueView
              feedback={data.feedback}
              assetImpactReminders={data.assetImpactReminders}
              assetImpactReminderEvents={assetImpactEventsByReminder}
              assetImpactBusyId={assetImpactBusyId}
              assetImpactError={assetImpactError}
              onDecide={handleFeedbackDecision}
              onRetry={handleFeedbackRetry}
              onLoadAssetImpactEvents={handleLoadAssetImpactEvents}
              onResolveAssetImpactReminder={handleResolveAssetImpactReminder}
              onOpenNodeInGraph={handleSelectNodeFromReview}
              onOpenEdgeInGraph={handleSelectEdgeFromReview}
            />
          ) : null}
          {view === "assets" && data ? (
            <AssetInboxView
              assetInbox={data.assetInbox}
              projectId={currentProjectId}
              snapshotId={data.status?.active_snapshot_id ?? data.summary?.snapshot_id ?? ""}
            />
          ) : null}
          {view === "backlog" && data ? (
            <BacklogView
              backlog={data.backlog}
              projectId={currentProjectId}
              snapshotId={data.status?.active_snapshot_id ?? data.summary?.snapshot_id ?? ""}
              nodes={data.nodes}
            />
          ) : null}
          {!data && !error && view !== "projects" ? (
            <div className="view">
              <div className="empty">
                <span className="spinner" /> Loading governance snapshot…
              </div>
            </div>
          ) : null}
        </main>
      </div>
      {toast ? (
        <div className={`toast ${toast.kind}`} role="status">
          {toast.msg}
        </div>
      ) : null}
      {aiConfigOpen ? (
        <AiConfigDialog
          config={aiConfig}
          projectId={currentProjectId}
          projectLabel={projectDisplayName(projects, currentProjectId)}
          onSaved={(next) => setAiConfig(next)}
          onClose={() => setAiConfigOpen(false)}
        />
      ) : null}
      <ActionControlPanel
        open={actionPanel != null}
        kind={actionPanel?.kind ?? "enrich"}
        target={actionPanel?.target ?? null}
        snapshotId={data?.status.active_snapshot_id ?? data?.summary.snapshot_id ?? null}
        aiConfig={aiConfig}
        onClose={() => setActionPanel(null)}
        onSubmitted={(msg, kind) => setToast({ kind, msg })}
      />
      <ActionPanel
        open={actionPanelOpen}
        snapshotId={data?.status.active_snapshot_id ?? data?.summary.snapshot_id ?? null}
        feedback={data?.feedback ?? null}
        initialTab={actionPanelInitialTab}
        prefillDraft={actionPanelPrefill}
        onClose={() => {
          setActionPanelOpen(false);
          setActionPanelPrefill(null);
          setActionPanelInitialTab("review");
        }}
        onOpenPreset={handleOpenPreset}
        onOpenReviewView={() => {
          setActionPanelOpen(false);
          setView("review");
        }}
        onSubmitted={(msg, kind) => setToast({ kind, msg })}
        onRunReconcile={() => {
          setActionPanelOpen(false);
          handleQueueReconcile();
        }}
      />
    </div>
  );
}

function countOpenBacklog(backlog?: BacklogResponse): number {
  if (typeof backlog?.summary?.open === "number") return backlog.summary.open;
  return (
    backlog?.bugs?.filter((bug) => {
      const status = String(bug.status || "OPEN").toUpperCase();
      return !CLOSED_BACKLOG_STATUSES.has(status);
    }).length ?? 0
  );
}

function projectDisplayName(projects: ProjectListItem[], projectId: string): string {
  const project = projects.find((p) => p.project_id === projectId);
  return project?.name?.trim() || projectId;
}

function semanticAiReadiness(config?: AiConfigResponse | null): { ready: boolean; blockMessage: string } {
  const projectRoute = config?.project_config?.ai?.routing?.semantic;
  const provider = (projectRoute?.provider || "").trim();
  const model = (projectRoute?.model || "").trim();
  if (!provider || !model) {
    return {
      ready: false,
      blockMessage: "AI enrich blocked: configure this project's semantic provider/model in AI config first.",
    };
  }
  const tool = config?.tool_health?.[provider];
  if (!tool) {
    return {
      ready: false,
      blockMessage: `AI enrich blocked: no local tool requirement is registered for provider ${provider}.`,
    };
  }
  if (tool.status !== "detected") {
    return {
      ready: false,
      blockMessage:
        `AI enrich blocked: ${tool.runtime || provider} is ${tool.status || "not detected"}. ` +
        `Install/configure ${tool.command || provider} or choose another provider.`,
    };
  }
  return { ready: true, blockMessage: "" };
}

function AiConfigDialog({
  config,
  projectId,
  projectLabel,
  onSaved,
  onClose,
}: {
  config: AiConfigResponse | null;
  projectId: string;
  projectLabel: string;
  onSaved(config: AiConfigResponse): void;
  onClose(): void;
}) {
  const projectRouting = config?.project_config?.ai?.routing ?? {};
  const roleRouting = config?.role_routing ?? {};
  const modelCatalog = config?.model_catalog?.models ?? DEFAULT_AI_MODELS;
  const providerIds = Array.from(new Set([...Object.keys(DEFAULT_AI_MODELS), ...Object.keys(modelCatalog)]));
  const roles = Array.from(
    new Set([
      ...Object.keys(projectRouting),
      ...Object.keys(roleRouting),
      "pm",
      "dev",
      "tester",
      "qa",
      "semantic",
    ]),
  );
  const [draft, setDraft] = useState<Record<string, { provider: string; model: string }>>({});
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const semanticReadiness = semanticAiReadiness(config);
  const writeTarget =
    config?.write_target ||
    config?.project_config?.write_target ||
    "aming-claw project registry";
  const projectConfigSource =
    config?.project_config_source ||
    config?.project_config?.config_source ||
    "";
  const semanticSource =
    config?.semantic?.override_path ||
    (projectRouting.semantic ? writeTarget : config?.semantic?.source_path) ||
    "";

  const effectiveRouteFor = (role: string) => {
    const draftRoute = draft[role];
    if (draftRoute?.provider || draftRoute?.model) {
      return { provider: draftRoute.provider, model: draftRoute.model, source: "project draft" };
    }
    const projectRoute = projectRouting[role];
    if (projectRoute?.provider || projectRoute?.model) {
      return { ...projectRoute, source: projectConfigSource || "aming-claw registry" };
    }
    const fallback = role === "semantic" ? config?.semantic : roleRouting[role];
    return fallback ? { ...fallback, source: role === "semantic" ? "semantic default" : "global default" } : null;
  };

  useEffect(() => {
    setDraft(
      Object.fromEntries(
        roles.map((role) => [
          role,
          {
            provider: projectRouting[role]?.provider ?? "",
            model: projectRouting[role]?.model ?? "",
          },
        ]),
      ),
    );
  }, [config?.project_id, roles.join("\u0000")]);

  const updateDraft = (role: string, field: "provider" | "model", value: string) => {
    setDraft((current) => ({
      ...current,
      [role]: {
        provider: current[role]?.provider ?? "",
        model: current[role]?.model ?? "",
        [field]: value,
      },
    }));
  };

  const modelOptionsFor = (provider: string) => {
    const key = provider.trim();
    return key ? (modelCatalog[key] ?? DEFAULT_AI_MODELS[key] ?? []) : [];
  };

  const saveRouting = async () => {
    setSaving(true);
    setMessage(null);
    try {
      const routingEntries: Array<[string, { provider: string; model: string }]> = roles.map((role) => [
        role,
        {
          provider: (draft[role]?.provider ?? "").trim(),
          model: (draft[role]?.model ?? "").trim(),
        },
      ]);
      const routing = Object.fromEntries(
        routingEntries.filter(([, route]) => route.provider || route.model),
      );
      const next = await api.updateAiConfigFor(projectId, { routing, actor: "dashboard" });
      onSaved(next);
      setMessage({ kind: "success", text: "Saved AI routing." });
    } catch (error) {
      const msg = error instanceof ApiError ? `${error.message}${error.body ? ` ${error.body}` : ""}` : String(error);
      setMessage({ kind: "error", text: `Save failed: ${msg}` });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="AI configuration">
      <div className="config-dialog">
        <div className="config-dialog-head">
          <div>
            <div className="config-dialog-title">AI configuration</div>
            <div className="config-dialog-sub">
              {projectLabel} <span className="mono">· {projectId}</span>
            </div>
          </div>
          <button className="icon-btn" onClick={onClose} title="Close AI configuration">
            ×
          </button>
        </div>
        <div className="config-section">
          <div className="config-section-title">Project scope</div>
          <div className="config-kv">
            <span>Workspace</span>
            <span className="mono">{config?.workspace_path || "—"}</span>
            <span>Stores</span>
            <span className="mono">{writeTarget || "—"}</span>
            <span>Semantic source</span>
            <span className="mono">{semanticSource || "unset"}</span>
          </div>
          <div className={`config-warning ${semanticReadiness.ready ? "success" : "error"}`}>
            {semanticReadiness.ready
              ? "Live AI semantic jobs are enabled for this project route."
              : semanticReadiness.blockMessage}
          </div>
        </div>
        <div className="config-section">
          <div className="config-section-title">Project routing</div>
          <div className="config-dialog-sub" style={{ marginBottom: 10 }}>
            Anthropic routes through Claude Code (<span className="mono">claude</span> CLI). OpenAI routes through
            Codex CLI (<span className="mono">codex</span>). Version checks below are local tool probes only and do not
            call a model.
          </div>
          <div className="config-table">
            <div className="config-row config-row-head config-row-editable">
              <span>Role</span>
              <span>Provider</span>
              <span>Model</span>
              <span>Effective</span>
            </div>
            {roles.map((role) => {
              const effective = effectiveRouteFor(role);
              const provider = draft[role]?.provider ?? "";
              const modelOptions = modelOptionsFor(provider);
              const currentModel = draft[role]?.model ?? "";
              return (
                <div className="config-row config-row-editable" key={role}>
                  <span className="mono">{role}</span>
                  <select
                    value={provider}
                    onChange={(event) => updateDraft(role, "provider", event.target.value)}
                  >
                    <option value="">provider</option>
                    {providerIds.map((id) => (
                      <option key={id} value={id}>
                        {id}
                      </option>
                    ))}
                  </select>
                  <select
                    value={modelOptions.includes(currentModel) ? currentModel : currentModel ? "__custom__" : ""}
                    onChange={(event) => {
                      const value = event.target.value;
                      updateDraft(role, "model", value === "__custom__" ? currentModel : value);
                    }}
                  >
                    <option value="">model</option>
                    {modelOptions.map((model) => (
                      <option key={model} value={model}>
                        {model}
                      </option>
                    ))}
                    {currentModel && !modelOptions.includes(currentModel) ? (
                      <option value="__custom__">custom: {currentModel}</option>
                    ) : null}
                  </select>
                  <span title={effective?.source || ""}>
                    {fmtRoute(effective)}{" "}
                    {effective?.source ? <span className="config-source-chip">{effective.source}</span> : null}
                  </span>
                </div>
              );
            })}
          </div>
          <div className="config-dialog-actions">
            <button className="action-btn action-btn-primary" disabled={saving || !config} onClick={saveRouting}>
              {saving ? "Saving..." : "Save routing"}
            </button>
            <span className="config-dialog-sub">
              {config?.write_supported === false ? "write disabled" : "writes Aming-claw registry"}
            </span>
          </div>
          {message ? <div className={`config-warning ${message.kind}`}>{message.text}</div> : null}
        </div>
        <div className="config-section">
          <div className="config-section-title">Local AI tools</div>
          <div className="config-kv">
            {providerIds.map((provider) => {
              const tool = config?.tool_health?.[provider];
              const providerMeta = config?.model_catalog?.providers?.[provider];
              const status = tool?.status ?? "unknown";
              return (
                <Fragment key={provider}>
                  <span>{provider}</span>
                  <span>
                    <span className={`status-pill ${status === "detected" ? "ok" : status === "missing" ? "err" : "warn"}`}>
                      {status}
                    </span>{" "}
                    <span className="mono">
                      {tool?.runtime ?? providerMeta?.runtime ?? providerMeta?.command ?? "local CLI"}
                      {tool?.version ? ` · ${tool.version}` : ""}
                    </span>
                    {tool?.path ? <span className="config-dialog-sub"> · {tool.path}</span> : null}
                  </span>
                </Fragment>
              );
            })}
          </div>
        </div>
        <div className="config-section">
          <div className="config-section-title">Semantic worker</div>
          <div className="config-kv">
            <span>Analyzer role</span>
            <span className="mono">{config?.semantic?.analyzer_role ?? "—"}</span>
            <span>Chain role</span>
            <span className="mono">{config?.semantic?.chain_role ?? "—"}</span>
            <span>Default AI</span>
            <span>{config?.semantic?.use_ai_default ? "enabled" : "manual"}</span>
          </div>
        </div>
        {config?.pipeline_error || config?.semantic_error || config?.project_config_error ? (
          <div className="config-warning">
            {[config.pipeline_error, config.semantic_error, config.project_config_error]
              .filter(Boolean)
              .join(" · ")}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function fmtRoute(route?: { provider?: string; model?: string } | null): string {
  if (!route) return "—";
  const provider = route.provider || "default";
  const model = route.model || "default";
  return `${provider}/${model}`;
}
