import type {
  ActiveSummaryResponse,
  AssetImpactReminderEventsResponse,
  AssetImpactReminderResolveResponse,
  AssetImpactRemindersResponse,
  AssetImpactResolutionKind,
  AssetDriftProposalResponse,
  AssetDriftStateResponse,
  AssetInboxResponse,
  AttachFileHintResponse,
  BacklogTimelineGateResponse,
  BacklogBug,
  BacklogResponse,
  EdgesResponse,
  FeedbackQueueResponse,
  FileHygieneActionResponse,
  SnapshotFilesResponse,
  HealthResponse,
  NodesResponse,
  ObserverCommand,
  OperationsQueueResponse,
  ProjectInboxResponse,
  ProjectionResponse,
  RawRequirement,
  StatusResponse,
  TaskTimelineResponse,
  UnbindFileHintResponse,
} from "../types";

const DEFAULT_PROJECT_ID = (import.meta.env.VITE_PROJECT_ID as string | undefined) || "aming-claw";
const DIRECT = (import.meta.env.VITE_DIRECT_API as string | undefined) === "true";
const BACKEND = (import.meta.env.VITE_BACKEND_URL as string | undefined) || "http://localhost:40000";

let activeProjectId = DEFAULT_PROJECT_ID;

export const projectId = DEFAULT_PROJECT_ID;

export function getProjectId(): string {
  return activeProjectId;
}

export function setProjectId(projectId: string): void {
  activeProjectId = projectId.trim() || DEFAULT_PROJECT_ID;
}

function pid(): string {
  return pidFor(activeProjectId);
}

function pidFor(projectId: string): string {
  return encodeURIComponent(projectId.trim() || DEFAULT_PROJECT_ID);
}

function backlogListQuery(): string {
  return new URLSearchParams({
    view: "compact",
    limit: "200",
    offset: "0",
    include_closed: "true",
  }).toString();
}

function backlogTimelineQuery(backlogId: string, limit: number): string {
  return new URLSearchParams({
    backlog_id: backlogId,
    limit: String(limit),
  }).toString();
}

function backlogTimelineGateQuery(limit: number): string {
  return new URLSearchParams({
    include_events: "true",
    limit: String(limit),
  }).toString();
}

function assetImpactReminderQuery(opts: { asset_kind?: string; status?: string } = {}): string {
  const q = new URLSearchParams();
  q.set("asset_kind", opts.asset_kind ?? "");
  q.set("status", opts.status ?? "pending");
  return q.toString();
}

function base(): string {
  return DIRECT ? BACKEND : "";
}

async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const url = `${base()}${path}`;
  const res = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, `GET ${path} → ${res.status}`, text);
  }
  return (await res.json()) as T;
}

async function postJSON<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const url = `${base()}${path}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: body == null ? undefined : JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, `POST ${path} → ${res.status}`, text);
  }
  return (await res.json()) as T;
}

export class ApiError extends Error {
  status: number;
  body: string;
  constructor(status: number, message: string, body: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export const api = {
  health(signal?: AbortSignal) {
    return getJSON<HealthResponse>("/api/health", signal);
  },
  projects(signal?: AbortSignal) {
    return getJSON<ProjectsResponse>("/api/projects", signal);
  },
  projectConfig(signal?: AbortSignal) {
    return getJSON<ProjectConfigResponse>(`/api/projects/${pid()}/config`, signal);
  },
  projectConfigFor(projectId: string, signal?: AbortSignal) {
    return getJSON<ProjectConfigResponse>(`/api/projects/${pidFor(projectId)}/config`, signal);
  },
  projectE2EConfigFor(projectId: string, signal?: AbortSignal) {
    return getJSON<{ ok?: boolean; project_id: string; workspace_path?: string; e2e?: E2EConfig }>(
      `/api/projects/${pidFor(projectId)}/e2e/config`,
      signal,
    );
  },
  e2eImpact(snapshotId: string, signal?: AbortSignal) {
    return getJSON<E2EImpactResponse>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/e2e/impact`,
      signal,
    );
  },
  e2eImpactFor(projectId: string, snapshotId: string, signal?: AbortSignal) {
    return getJSON<E2EImpactResponse>(
      `/api/graph-governance/${pidFor(projectId)}/snapshots/${encodeURIComponent(snapshotId)}/e2e/impact`,
      signal,
    );
  },
  bootstrapProject(
    payload: {
      workspace_path: string;
      project_name?: string;
      scan_depth?: number;
      exclude_patterns?: string[];
      config_override?: { graph?: { exclude_paths?: string[]; ignore_globs?: string[] } };
    },
    signal?: AbortSignal,
  ) {
    return postJSON<BootstrapProjectResponse>("/api/project/bootstrap", payload, signal);
  },
  chooseLocalDirectory(
    payload: { initial_path?: string; title?: string; timeout_seconds?: number } = {},
    signal?: AbortSignal,
  ) {
    return postJSON<LocalDirectoryPickerResponse>("/api/local/choose-directory", payload, signal);
  },
  aiConfig(signal?: AbortSignal) {
    return getJSON<AiConfigResponse>(`/api/projects/${pid()}/ai-config`, signal);
  },
  aiConfigFor(projectId: string, signal?: AbortSignal) {
    return getJSON<AiConfigResponse>(`/api/projects/${pidFor(projectId)}/ai-config`, signal);
  },
  updateAiConfigFor(projectId: string, payload: AiConfigUpdatePayload, signal?: AbortSignal) {
    return postJSON<AiConfigResponse>(`/api/projects/${pidFor(projectId)}/ai-config`, payload, signal);
  },
  gitRefsFor(projectId: string, signal?: AbortSignal) {
    return getJSON<ProjectGitRefsResponse>(`/api/projects/${pidFor(projectId)}/git-refs`, signal);
  },
  selectGitRefFor(projectId: string, payload: ProjectGitRefSelectPayload, signal?: AbortSignal) {
    return postJSON<ProjectGitRefsResponse>(`/api/projects/${pidFor(projectId)}/git-ref`, payload, signal);
  },
  projectInboxFor(projectId: string, signal?: AbortSignal) {
    return getJSON<ProjectInboxResponse>(`/api/projects/${pidFor(projectId)}/project-inbox`, signal);
  },
  captureRawRequirementFor(
    projectId: string,
    payload: {
      raw_text: string;
      source?: string;
      session_id?: string;
      actor?: string;
      metadata?: Record<string, unknown>;
    },
    signal?: AbortSignal,
  ) {
    return postJSON<{ ok: boolean; project_id: string; raw_requirement: RawRequirement; created_backlog: false }>(
      `/api/projects/${pidFor(projectId)}/raw-requirements`,
      payload,
      signal,
    );
  },
  updateRawRequirementStatusFor(
    projectId: string,
    rawId: string,
    payload: { status: string; note?: string; promoted_bug_id?: string; bug_id?: string },
    signal?: AbortSignal,
  ) {
    return postJSON<{ ok: boolean; project_id: string; raw_requirement: RawRequirement }>(
      `/api/projects/${pidFor(projectId)}/raw-requirements/${encodeURIComponent(rawId)}/status`,
      payload,
      signal,
    );
  },
  enqueueObserverCommandFor(
    projectId: string,
    payload: {
      command_type: string;
      payload?: Record<string, unknown>;
      target_session_id?: string;
      created_by?: string;
    },
    signal?: AbortSignal,
  ) {
    return postJSON<{ ok: boolean; project_id: string; observer_command: ObserverCommand }>(
      `/api/projects/${pidFor(projectId)}/observer-commands`,
      payload,
      signal,
    );
  },
  status(signal?: AbortSignal) {
    return getJSON<StatusResponse>(`/api/graph-governance/${pid()}/status`, signal);
  },
  statusFor(projectId: string, signal?: AbortSignal) {
    return getJSON<StatusResponse>(`/api/graph-governance/${pidFor(projectId)}/status`, signal);
  },
  activeSummary(signal?: AbortSignal) {
    return getJSON<ActiveSummaryResponse>(
      `/api/graph-governance/${pid()}/snapshots/active/summary`,
      signal,
    );
  },
  activeSummaryFor(projectId: string, signal?: AbortSignal) {
    return getJSON<ActiveSummaryResponse>(
      `/api/graph-governance/${pidFor(projectId)}/snapshots/active/summary`,
      signal,
    );
  },
  assetInbox(snapshotId: string, signal?: AbortSignal) {
    return getJSON<AssetInboxResponse>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/asset-inbox`,
      signal,
    );
  },
  assetInboxFor(projectId: string, snapshotId: string, signal?: AbortSignal) {
    return getJSON<AssetInboxResponse>(
      `/api/graph-governance/${pidFor(projectId)}/snapshots/${encodeURIComponent(snapshotId)}/asset-inbox`,
      signal,
    );
  },
  activeAssetInbox(signal?: AbortSignal) {
    return getJSON<AssetInboxResponse>(
      `/api/graph-governance/${pid()}/snapshots/active/asset-inbox`,
      signal,
    );
  },
  activeAssetInboxFor(projectId: string, signal?: AbortSignal) {
    return getJSON<AssetInboxResponse>(
      `/api/graph-governance/${pidFor(projectId)}/snapshots/active/asset-inbox`,
      signal,
    );
  },
  assetImpactReminders(
    opts: { asset_kind?: string; status?: string } = {},
    signal?: AbortSignal,
  ) {
    return getJSON<AssetImpactRemindersResponse>(
      `/api/graph-governance/${pid()}/asset-impact/reminders?${assetImpactReminderQuery(opts)}`,
      signal,
    );
  },
  assetImpactRemindersFor(
    projectId: string,
    opts: { asset_kind?: string; status?: string } = {},
    signal?: AbortSignal,
  ) {
    return getJSON<AssetImpactRemindersResponse>(
      `/api/graph-governance/${pidFor(projectId)}/asset-impact/reminders?${assetImpactReminderQuery(opts)}`,
      signal,
    );
  },
  assetImpactReminderEventsFor(projectId: string, reminderId: string, signal?: AbortSignal) {
    return getJSON<AssetImpactReminderEventsResponse>(
      `/api/graph-governance/${pidFor(projectId)}/asset-impact/reminders/${encodeURIComponent(reminderId)}/events`,
      signal,
    );
  },
  resolveAssetImpactReminderFor(
    projectId: string,
    reminderId: string,
    payload: { resolution_kind: AssetImpactResolutionKind; note: string; actor: string },
    signal?: AbortSignal,
  ) {
    return postJSON<AssetImpactReminderResolveResponse>(
      `/api/graph-governance/${pidFor(projectId)}/asset-impact/reminders/${encodeURIComponent(reminderId)}/resolve`,
      payload,
      signal,
    );
  },
  recordAssetDriftStateFor(
    projectId: string,
    payload: {
      asset_kind: string;
      asset_path: string;
      drift_state: string;
      snapshot_id?: string;
      commit_sha?: string;
      actor?: string;
      evidence?: Record<string, unknown>;
    },
    signal?: AbortSignal,
  ) {
    return postJSON<AssetDriftStateResponse>(
      `/api/graph-governance/${pidFor(projectId)}/asset-drift/state`,
      { ...payload, actor: payload.actor ?? "dashboard_user" },
      signal,
    );
  },
  queueAssetDriftProposalFor(
    projectId: string,
    payload: {
      asset_kind: string;
      asset_path: string;
      snapshot_id: string;
      commit_sha?: string;
      node_id?: string;
      actor?: string;
      note?: string;
      mode?: string;
    },
    signal?: AbortSignal,
  ) {
    return postJSON<AssetDriftProposalResponse>(
      `/api/graph-governance/${pidFor(projectId)}/asset-drift/proposals`,
      { ...payload, actor: payload.actor ?? "dashboard_user" },
      signal,
    );
  },
  activeProjection(signal?: AbortSignal) {
    return getJSON<ProjectionResponse>(
      `/api/graph-governance/${pid()}/snapshots/active/semantic/projection`,
      signal,
    );
  },
  activeProjectionFor(projectId: string, signal?: AbortSignal) {
    return getJSON<ProjectionResponse>(
      `/api/graph-governance/${pidFor(projectId)}/snapshots/active/semantic/projection`,
      signal,
    );
  },
  nodes(snapshotId: string, limit = 1000, signal?: AbortSignal) {
    const path =
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}` +
      `/nodes?include_semantic=true&limit=${limit}`;
    return getJSON<NodesResponse>(path, signal);
  },
  nodesFor(projectId: string, snapshotId: string, limit = 1000, signal?: AbortSignal) {
    const path =
      `/api/graph-governance/${pidFor(projectId)}/snapshots/${encodeURIComponent(snapshotId)}` +
      `/nodes?include_semantic=true&limit=${limit}`;
    return getJSON<NodesResponse>(path, signal);
  },
  edges(snapshotId: string, limit = 4000, signal?: AbortSignal) {
    const path =
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}` +
      `/edges?limit=${limit}`;
    return getJSON<EdgesResponse>(path, signal);
  },
  edgesFor(projectId: string, snapshotId: string, limit = 4000, signal?: AbortSignal) {
    const path =
      `/api/graph-governance/${pidFor(projectId)}/snapshots/${encodeURIComponent(snapshotId)}` +
      `/edges?limit=${limit}`;
    return getJSON<EdgesResponse>(path, signal);
  },
  operationsQueue(signal?: AbortSignal) {
    return getJSON<OperationsQueueResponse>(
      `/api/graph-governance/${pid()}/operations/queue`,
      signal,
    );
  },
  operationsQueueFor(projectId: string, signal?: AbortSignal) {
    return getJSON<OperationsQueueResponse>(
      `/api/graph-governance/${pidFor(projectId)}/operations/queue`,
      signal,
    );
  },
  backlog(signal?: AbortSignal) {
    return getJSON<BacklogResponse>(`/api/backlog/${pid()}?${backlogListQuery()}`, signal);
  },
  backlogFor(projectId: string, signal?: AbortSignal) {
    return getJSON<BacklogResponse>(`/api/backlog/${pidFor(projectId)}?${backlogListQuery()}`, signal);
  },
  backlogBugFor(projectId: string, backlogId: string, signal?: AbortSignal) {
    return getJSON<BacklogBug>(
      `/api/backlog/${pidFor(projectId)}/${encodeURIComponent(backlogId)}`,
      signal,
    );
  },
  taskTimelineFor(projectId: string, backlogId: string, limit = 50, signal?: AbortSignal) {
    const q = backlogTimelineQuery(backlogId, limit);
    return getJSON<TaskTimelineResponse>(`/api/task/${pidFor(projectId)}/timeline?${q}`, signal);
  },
  backlogTimelineGateFor(projectId: string, backlogId: string, limit = 50, signal?: AbortSignal) {
    const q = backlogTimelineGateQuery(limit);
    return getJSON<BacklogTimelineGateResponse>(
      `/api/backlog/${pidFor(projectId)}/${encodeURIComponent(backlogId)}/timeline-gate?${q}`,
      signal,
    );
  },
  snapshotFiles(
    snapshotId: string,
    opts: { limit?: number; scan_status?: string; file_kind?: string; sort?: string } = {},
    signal?: AbortSignal,
  ) {
    const q = new URLSearchParams();
    q.set("limit", String(opts.limit ?? 1000));
    if (opts.scan_status) q.set("scan_status", opts.scan_status);
    if (opts.file_kind) q.set("file_kind", opts.file_kind);
    if (opts.sort) q.set("sort", opts.sort);
    return getJSON<SnapshotFilesResponse>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/files?${q.toString()}`,
      signal,
    );
  },
  attachFileGovernanceHint(
    snapshotId: string,
    payload: { path: string; target_node_id: string; role?: "doc" | "test" | "config"; actor?: string },
    signal?: AbortSignal,
  ) {
    return postJSON<AttachFileHintResponse>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/file-hygiene/hints/attach`,
      { ...payload, actor: payload.actor ?? "dashboard_user" },
      signal,
    );
  },
  attachFileGovernanceHintFor(
    projectId: string,
    snapshotId: string,
    payload: { path: string; target_node_id: string; role?: "doc" | "test" | "config"; actor?: string },
    signal?: AbortSignal,
  ) {
    return postJSON<AttachFileHintResponse>(
      `/api/graph-governance/${pidFor(projectId)}/snapshots/${encodeURIComponent(snapshotId)}/file-hygiene/hints/attach`,
      { ...payload, actor: payload.actor ?? "dashboard_user" },
      signal,
    );
  },
  unbindFileGovernanceHintFor(
    projectId: string,
    snapshotId: string,
    payload: {
      path: string;
      target_node_id: string;
      role?: "doc" | "test" | "config";
      reason: string;
      actor?: string;
      dry_run?: boolean;
    },
    signal?: AbortSignal,
  ) {
    return postJSON<UnbindFileHintResponse>(
      `/api/graph-governance/${pidFor(projectId)}/snapshots/${encodeURIComponent(snapshotId)}/file-hygiene/hints/unbind`,
      { ...payload, actor: payload.actor ?? "dashboard_user" },
      signal,
    );
  },
  fileHygieneActionFor(
    projectId: string,
    snapshotId: string,
    payload: {
      action: string;
      path: string;
      target_node_id?: string;
      role?: "doc" | "test" | "config";
      actor?: string;
      reason?: string;
      operator_signoff?: boolean;
      confirm_delete_candidate?: boolean;
    },
    signal?: AbortSignal,
  ) {
    return postJSON<FileHygieneActionResponse>(
      `/api/graph-governance/${pidFor(projectId)}/snapshots/${encodeURIComponent(snapshotId)}/file-hygiene/actions`,
      { ...payload, actor: payload.actor ?? "dashboard_user" },
      signal,
    );
  },
  fullReconcileFor(projectId: string, payload: GraphReconcilePayload, signal?: AbortSignal) {
    return postJSON<GraphReconcileResponse>(
      `/api/graph-governance/${pidFor(projectId)}/reconcile/full`,
      payload,
      signal,
    );
  },
  queuePendingScopeFor(projectId: string, payload: PendingScopePayload, signal?: AbortSignal) {
    return postJSON<{ ok?: boolean; pending_scope_reconcile?: unknown }>(
      `/api/graph-governance/${pidFor(projectId)}/pending-scope`,
      payload,
      signal,
    );
  },
  materializePendingScopeFor(projectId: string, payload: GraphReconcilePayload, signal?: AbortSignal) {
    return postJSON<GraphReconcileResponse>(
      `/api/graph-governance/${pidFor(projectId)}/reconcile/pending-scope`,
      payload,
      signal,
    );
  },
  feedbackQueue(snapshotId: string, signal?: AbortSignal) {
    // MF-2026-05-10-016 P1: drop require_current_semantic filter so the
    // dashboard surfaces every needs_observer_decision item the operator can
    // act on. The semantic_review_gate.reason on each group still tells the UI
    // whether the underlying semantic is current.
    const path =
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}` +
      `/feedback/queue?require_current_semantic=false`;
    return getJSON<FeedbackQueueResponse>(path, signal);
  },
  feedbackQueueFor(projectId: string, snapshotId: string, signal?: AbortSignal) {
    const path =
      `/api/graph-governance/${pidFor(projectId)}/snapshots/${encodeURIComponent(snapshotId)}` +
      `/feedback/queue?require_current_semantic=false`;
    return getJSON<FeedbackQueueResponse>(path, signal);
  },
  decideFeedback(
    snapshotId: string,
    payload: {
      feedback_ids: string[];
      action: string;
      actor?: string;
      rationale?: string;
    },
    signal?: AbortSignal,
  ) {
    // When the operator clicks any of the accept_* actions, that click IS the
    // human signoff — pass accept=true so the backend doesn't fall back to
    // requires_human_signoff (which would leave the row in an intermediate
    // needs_human_signoff state and the UI looks like nothing happened).
    // Reject and Defer don't set the flag — let the backend interpret those
    // as the operator declining to sign off.
    const isAccept = payload.action.startsWith("accept_");
    return postJSON<{
      ok?: boolean;
      decided_count?: number;
      error_count?: number;
      semantic_enrichment_accepted?: {
        node_ids_flipped?: string[];
        event_ids_flipped?: string[];
      };
      projection_rebuilt?: boolean;
      projection_rebuild_error?: string;
    }>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/feedback/decision`,
      {
        feedback_ids: payload.feedback_ids,
        action: payload.action,
        actor: payload.actor ?? "dashboard_user",
        rationale: payload.rationale ?? "",
        ...(isAccept ? { accept: true } : {}),
      },
      signal,
    );
  },
  cancelGraphStructureFeedback(
    snapshotId: string,
    payload: {
      feedback_ids: string[];
      files?: string[];
      reason?: string;
      actor?: string;
    },
    signal?: AbortSignal,
  ) {
    return api.cancelGraphStructureFeedbackFor(pid(), snapshotId, payload, signal);
  },
  cancelGraphStructureFeedbackFor(
    projectId: string,
    snapshotId: string,
    payload: {
      feedback_ids: string[];
      files?: string[];
      reason?: string;
      actor?: string;
    },
    signal?: AbortSignal,
  ) {
    return postJSON<{
      ok: boolean;
      status: string;
      changed_files?: string[];
      discarded_files?: string[];
      dirty_guard?: Record<string, unknown>;
      message?: string;
    }>(
      `/api/graph-governance/${pidFor(projectId)}/snapshots/${encodeURIComponent(snapshotId)}/feedback/graph-structure/cancel`,
      {
        feedback_ids: payload.feedback_ids,
        files: payload.files ?? [],
        reason: payload.reason ?? "dashboard_cancel_graph_structure_operation",
        actor: payload.actor ?? "dashboard_user",
      },
      signal,
    );
  },
  commitGraphStructureFeedback(
    snapshotId: string,
    payload: {
      feedback_ids: string[];
      files?: string[];
      reason?: string;
      message?: string;
      actor?: string;
    },
    signal?: AbortSignal,
  ) {
    return api.commitGraphStructureFeedbackFor(pid(), snapshotId, payload, signal);
  },
  commitGraphStructureFeedbackFor(
    projectId: string,
    snapshotId: string,
    payload: {
      feedback_ids: string[];
      files?: string[];
      reason?: string;
      message?: string;
      actor?: string;
    },
    signal?: AbortSignal,
  ) {
    return postJSON<{
      ok: boolean;
      status: string;
      changed_files?: string[];
      dirty_guard?: Record<string, unknown>;
      commit?: { commit_sha?: string; files?: string[] };
      requires_update_graph?: boolean;
      message?: string;
    }>(
      `/api/graph-governance/${pidFor(projectId)}/snapshots/${encodeURIComponent(snapshotId)}/feedback/graph-structure/commit`,
      {
        feedback_ids: payload.feedback_ids,
        files: payload.files ?? [],
        reason: payload.reason ?? "dashboard_commit_graph_structure_operation",
        message: payload.message ?? "manual fix: apply graph-structure review operation",
        actor: payload.actor ?? "dashboard_user",
      },
      signal,
    );
  },
  // Legacy explicit queue API. Normal dashboard Update graph uses the direct
  // materialize endpoint below so users do not see a stale queued task.
  queueScopeReconcile(opts: { commit_sha: string; parent_commit_sha?: string; actor?: string }, signal?: AbortSignal) {
    return postJSON<{
      ok: boolean;
      pending_scope_reconcile?: {
        commit_sha: string;
        // queued / running / materialized / failed / waived — the upsert
        // preserves materialized & waived so a previously cancelled commit
        // returns its OLD status here, even though POST returned 201.
        status: string;
        retry_count?: number;
        queued_at?: string;
      };
    }>(
      `/api/graph-governance/${pid()}/pending-scope`,
      {
        commit_sha: opts.commit_sha,
        parent_commit_sha: opts.parent_commit_sha,
        actor: opts.actor ?? "dashboard_user",
        evidence: { source: "dashboard_stale_banner" },
      },
      signal,
    );
  },
  // Direct Update graph: materialize a scope candidate and activate it in one
  // round-trip. Backend creates/consumes transient pending-scope bookkeeping
  // when no queued row exists.
  // dry_run=false here means "really build the snapshot"; AI is opt-in via
  // semantic_use_ai (default false → rule-based + carry-forward only).
  materializeAndActivatePendingScope(
    opts: { target_commit_sha: string; parent_commit_sha?: string; semantic_use_ai?: boolean; actor?: string },
    signal?: AbortSignal,
  ) {
    return postJSON<{
      ok: boolean;
      snapshot_id: string;
      activation?: { snapshot_id?: string; previous_snapshot_id?: string; projection_status?: string };
    }>(
      `/api/graph-governance/${pid()}/reconcile/pending-scope`,
      {
        target_commit_sha: opts.target_commit_sha,
        parent_commit_sha: opts.parent_commit_sha,
        actor: opts.actor ?? "dashboard_user",
        semantic_use_ai: opts.semantic_use_ai ?? false,
        activate: true,
      },
      signal,
    );
  },
  cancelScopeReconcile(
    opts: { commit_sha?: string; operation_id?: string; actor?: string; reason?: string },
    signal?: AbortSignal,
  ) {
    return postJSON<{
      ok: boolean;
      status: "cancelled" | "not_found" | string;
      cancelled_count: number;
      waived_count?: number;
      operation_id?: string;
    }>(
      `/api/graph-governance/${pid()}/reconcile/scope/cancel`,
      {
        commit_sha: opts.commit_sha,
        operation_id: opts.operation_id,
        actor: opts.actor ?? "dashboard_user",
        reason: opts.reason ?? "dashboard_cancel",
      },
      signal,
    );
  },
  cancelSemanticJob(snapshotId: string, jobId: string, signal?: AbortSignal) {
    return postJSON<{
      ok: boolean;
      cancelled_count?: number;
      job?: { job_id?: string; status?: string };
    }>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs/${encodeURIComponent(jobId)}/cancel`,
      { actor: "dashboard_user" },
      signal,
    );
  },
  cancelAllSemanticJobs(
    snapshotId: string,
    filters: {
      operation_type?: "node_semantic" | "edge_semantic";
      target_scope?: "node" | "edge" | "subtree" | "snapshot";
      before_ts?: string;
      status?: "queued" | "running";
    },
    signal?: AbortSignal,
  ) {
    return postJSON<{
      ok: boolean;
      cancelled_count: number;
      skipped_terminal: number;
      matched_count?: number;
      cancelled_ops?: Array<{ operation_id: string; target_id: string }>;
    }>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs/cancel-all`,
      { ...filters, actor: "dashboard_user" },
      signal,
    );
  },
  clearTerminalSemanticJobs(
    snapshotId: string,
    opts: {
      operation_type?: "node_semantic" | "edge_semantic";
      before_ts?: string;
      statuses?: string[];
    },
    signal?: AbortSignal,
  ) {
    // MF-2026-05-10-011: physically deletes terminal node rows; edge events
    // stay as audit history (edge_audit_matched is informational).
    return postJSON<{
      ok: boolean;
      deleted_count: number;
      edge_audit_matched: number;
      requested_statuses: string[];
    }>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs/clear-terminal`,
      { ...opts, actor: "dashboard_user" },
      signal,
    );
  },
  cancelFeedback(snapshotId: string, opts: { feedback_ids?: string[]; limit?: number }, signal?: AbortSignal) {
    return postJSON<{
      ok: boolean;
      status: "soft_cancelled" | string;
      cancelled_count: number;
      feedback_cancel_contract?: "keep_status_observation";
    }>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/feedback/cancel`,
      { ...opts, actor: "dashboard_user" },
      signal,
    );
  },
  submitSemanticJob(snapshotId: string, payload: SemanticJobPayload, signal?: AbortSignal) {
    return postJSON<SemanticJobResponse>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs`,
      payload,
      signal,
    );
  },
  submitFeedback(snapshotId: string, payload: FeedbackSubmitPayload, signal?: AbortSignal) {
    return postJSON<FeedbackSubmitResponse>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/feedback`,
      payload,
      signal,
    );
  },
  // MF-016/017 review surface: fetch graph_events rows so the dashboard can
  // render the AI's candidate semantic_payload alongside the feedback row.
  // Filter to status=proposed + matching target to find pending review payloads.
  listProposedEvents(
    snapshotId: string,
    opts: { target_type: "node" | "edge"; target_id: string },
    signal?: AbortSignal,
  ) {
    const q = new URLSearchParams({
      target_type: opts.target_type,
      target_id: opts.target_id,
      status: "proposed",
      limit: "10",
    });
    return getJSON<{
      ok: boolean;
      count: number;
      events: Array<{
        event_id: string;
        event_type: string;
        target_type: string;
        target_id: string;
        status: string;
        confidence?: number;
        created_at?: string;
        payload?: {
          semantic_payload?: Record<string, unknown>;
          edge?: Record<string, unknown>;
          edge_context?: Record<string, unknown>;
          [key: string]: unknown;
        };
      }>;
    }>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/events?${q.toString()}`,
      signal,
    );
  },
  // POST /semantic-feedback — appends to the JSONL artifact that
  // run_semantic_enrichment reads (and pipes per-node into the AI payload's
  // `review_feedback` array). Separate from graph_feedback_items table.
  // Used by Retry: operator's rationale flows into the next AI call.
  appendSemanticFeedback(
    snapshotId: string,
    items: Array<{
      target_type: "node" | "edge" | "path" | "snapshot";
      target_id?: string;
      issue: string;
      priority?: "P0" | "P1" | "P2" | "P3";
      reason?: string;
      source_node_ids?: string[];
    }>,
    actor?: string,
    signal?: AbortSignal,
  ) {
    return postJSON<{
      ok: boolean;
      added_count?: number;
      feedback_path?: string;
    }>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/semantic-feedback`,
      { feedback_items: items, actor: actor ?? "dashboard_user" },
      signal,
    );
  },
  submitProposedEvent(snapshotId: string, payload: Record<string, unknown>, signal?: AbortSignal) {
    // Backend wraps the event row under `event`; older builds returned a flat
    // `event_id` field. Keep both shapes resilient.
    return postJSON<{
      ok: boolean;
      event?: { event_id?: string; status?: string };
      event_id?: string;
      status?: string;
    }>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/events`,
      payload,
      signal,
    );
  },
  fileBacklogFromEvent(
    snapshotId: string,
    eventId: string,
    payload: { backlog: BacklogDraft; start_chain?: boolean },
    signal?: AbortSignal,
  ) {
    // The endpoint returns `bug_id` + `event.backlog_bug_id` as the canonical
    // identifier. Older builds returned `backlog_task_id` / `task_id`; keep
    // both shapes resilient.
    return postJSON<{
      ok: boolean;
      bug_id?: string;
      event?: { backlog_bug_id?: string };
      backlog_task_id?: string;
      task_id?: string;
    }>(
      `/api/graph-governance/${pid()}/snapshots/${encodeURIComponent(snapshotId)}/events/${encodeURIComponent(eventId)}/file-backlog`,
      payload,
      signal,
    );
  },
};

export interface BacklogDraft {
  title: string;
  task_type: "pm" | "dev" | "test" | "qa" | "task" | "reconcile" | "mf";
  priority: "P0" | "P1" | "P2" | "P3";
  target_files: string[];
  affected_graph_nodes: string[];
  graph_gate_mode: "strict" | "advisory" | "raw";
  branch_mode: "main" | "batch_branch" | "reconcile_branch";
  acceptance_criteria: string[];
  prompt: string;
}

export interface ProjectListItem {
  project_id: string;
  name?: string;
  workspace_path?: string;
  status?: string;
  initialized?: boolean;
  node_count?: number;
  active_snapshot_id?: string;
  selected_ref?: string;
  selected_ref_updated_at?: string;
  bootstrap_progress?: ProjectOperationProgress;
  created_at?: string;
}

export interface ProjectOperationProgress {
  operation?: "bootstrap" | "build_graph" | "update_graph" | string;
  status?: "running" | "succeeded" | "failed" | "cancelled" | string;
  phase?: string;
  message?: string;
  started_at?: string;
  updated_at?: string;
  completed_at?: string;
  elapsed_seconds?: number;
  heartbeat?: number;
}

export interface ProjectsResponse {
  ok?: boolean;
  projects: ProjectListItem[];
}

export interface E2ESuiteConfig {
  suite_id?: string;
  label?: string;
  command?: string;
  trigger?: { paths?: string[]; nodes?: string[]; tags?: string[] };
  auto_run?: boolean;
  live_ai?: boolean;
  mutates_db?: boolean;
  requires_human_approval?: boolean;
  isolation_project?: string;
  timeout_sec?: number;
  max_parallel?: number;
}

export interface E2EConfig {
  auto_run?: boolean;
  default_timeout_sec?: number;
  max_parallel?: number;
  require_clean_worktree?: boolean;
  evidence_retention_days?: number;
  suites?: Record<string, E2ESuiteConfig>;
}

export interface E2EImpactResponse {
  ok?: boolean;
  project_id: string;
  snapshot_id: string;
  summary?: Record<string, number>;
  suites?: Array<{
    suite_id: string;
    label?: string;
    status: "current" | "stale" | "missing" | "failed" | "blocked" | string;
    required?: boolean;
    trigger_matched?: boolean;
    can_autorun?: boolean;
    blocked_reason?: string;
    command?: string;
    latest_evidence?: Record<string, unknown>;
    stale_reasons?: Array<Record<string, unknown>>;
  }>;
}

export interface ProjectConfigResponse {
  project_id: string;
  language: string;
  config_source?: string;
  write_target?: string;
  local_config_error?: string;
  testing?: { unit_command?: string; e2e_command?: string; e2e?: E2EConfig };
  graph?: {
    exclude_paths?: string[];
    ignore_globs?: string[];
    effective_exclude_roots?: string[];
    nested_projects?: { mode?: string; roots?: string[] };
  };
  ai?: { routing?: Record<string, { provider?: string; model?: string }> };
}

export interface BootstrapProjectResponse {
  project_id: string;
  snapshot_id?: string;
  graph_stats?: { node_count?: number; edge_count?: number };
  preflight?: { status?: string };
  activation?: { snapshot_id?: string; projection_status?: string };
}

export interface LocalDirectoryPickerResponse {
  ok?: boolean;
  selected: boolean;
  path?: string;
  manual_entry?: boolean;
  error?: string;
}

export interface GraphReconcilePayload {
  run_id?: string;
  target_commit_sha?: string;
  commit_sha?: string;
  parent_commit_sha?: string;
  actor?: string;
  activate?: boolean;
  semantic_enrich?: boolean;
  semantic_use_ai?: boolean;
  enqueue_stale?: boolean;
  semantic_skip_completed?: boolean;
  notes_extra?: Record<string, unknown>;
}

export interface GraphReconcileResponse {
  ok?: boolean;
  project_id?: string;
  run_id?: string;
  commit_sha?: string;
  snapshot_id?: string;
  snapshot_status?: string;
  activation?: { snapshot_id?: string; projection_status?: string };
  graph_stats?: { nodes?: number; edges?: number; node_count?: number; edge_count?: number };
}

export interface PendingScopePayload {
  commit_sha: string;
  parent_commit_sha?: string;
  actor?: string;
  evidence?: Record<string, unknown>;
}

export interface AiConfigResponse {
  project_id: string;
  workspace_path?: string;
  read_only?: boolean;
  write_supported?: boolean;
  project_config?: ProjectConfigResponse;
  project_config_source?: string;
  write_target?: string;
  role_routing?: Record<string, { provider?: string; model?: string; source?: string }>;
  tool_health?: Record<string, {
    provider?: string;
    label?: string;
    runtime?: string;
    command?: string;
    env_var?: string;
    path?: string;
    source?: string;
    status?: string;
    version?: string;
    auth_status?: string;
    error?: string;
  }>;
  model_catalog?: {
    providers?: Record<string, { label?: string; runtime?: string; command?: string; env_var?: string }>;
    models?: Record<string, string[]>;
  };
  semantic?: {
    provider?: string;
    model?: string;
    analyzer_role?: string;
    chain_role?: string;
    use_ai_default?: boolean;
    source_path?: string;
    override_path?: string;
    job_profiles?: Record<string, { provider?: string; model?: string; analyzer_role?: string }>;
  };
  pipeline_error?: string;
  semantic_error?: string;
  project_config_error?: string;
}

export interface AiConfigUpdatePayload {
  routing: Record<string, { provider?: string; model?: string }>;
  actor?: string;
}

export interface ProjectGitRefsResponse {
  ok?: boolean;
  project_id: string;
  workspace_path?: string;
  is_git_repo?: boolean;
  selected_ref?: string;
  current_branch?: string;
  head_commit?: string;
  branches?: string[];
  tags?: string[];
}

export interface ProjectGitRefSelectPayload {
  selected_ref: string;
  actor?: string;
}

export interface SemanticJobPayload {
  job_type: "semantic_enrichment" | "semantic_summary" | "global_review";
  target_scope: "snapshot" | "node" | "subtree" | "edge";
  target_ids: string[];
  options: {
    target?: "nodes" | "edges" | "both" | "summary";
    include_nodes?: boolean;
    include_edges?: boolean;
    scope?: string;
    mode?: "semanticize" | "retry" | "review";
    dry_run?: boolean;
    skip_current?: boolean;
    retry_stale_failed?: boolean;
    include_package_markers?: boolean;
    // Bulk-edge enrichment knobs (target_scope=edge with no target_ids).
    all_eligible?: boolean;
    include_contains?: boolean;
    edge_types?: string[];
    limit?: number;
    summary_source?: "child_semantics";
    require_current_children?: boolean;
    submit_for_review?: boolean;
  };
  created_by?: string;
}

export interface SemanticJobResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  job_id: string;
  status: string;
  queued_count?: number;
  operator_request?: unknown;
}

export interface FeedbackSubmitPayload {
  feedback_kind: string;
  summary: string;
  source_node_ids?: string[];
  target_id?: string;
  target_type?: "node" | "edge";
  priority?: "P0" | "P1" | "P2" | "P3" | "";
  paths?: string[];
  reason?: string;
  create_graph_event?: boolean;
  actor?: string;
  source_round?: string;
}

export interface FeedbackSubmitResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  // Single record shape (current backend, returns 201).
  feedback?: {
    feedback_id?: string;
    feedback_kind?: string;
    target_id?: string;
    target_type?: string;
    status?: string;
    issue?: string;
    issue_type?: string;
    confidence?: number;
    priority?: string;
  };
  event?: {
    event_id?: string;
    event_kind?: string;
    event_type?: string;
    status?: string;
    risk_level?: string;
  };
  // Legacy list shape — older builds returned `items: [...]`. Kept for resilience.
  items?: Array<{
    feedback_id?: string;
    feedback_kind?: string;
    target_id?: string;
    target_type?: string;
  }>;
  graph_event?: unknown;
}

export type Api = typeof api;
