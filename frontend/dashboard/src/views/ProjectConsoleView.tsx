import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import type {
  ActiveSummaryResponse,
  BacklogResponse,
  OperationsQueueResponse,
  StatusResponse,
} from "../types";
import {
  api,
  ApiError,
  type AiConfigResponse,
  type E2EImpactResponse,
  type ProjectConfigResponse,
  type ProjectGitRefsResponse,
  type ProjectListItem,
  type ProjectOperationProgress,
} from "../lib/api";

interface Props {
  projects: ProjectListItem[];
  currentProjectId: string;
  initialWorkspacePath?: string;
  loading: boolean;
  onOpenProject(projectId: string): void;
  onOpenAiConfig(): void;
  onRefresh(): Promise<void> | void;
}

interface ProjectRuntime {
  projectId: string;
  status?: StatusResponse;
  summary?: ActiveSummaryResponse;
  ops?: OperationsQueueResponse;
  backlog?: BacklogResponse;
  aiConfig?: AiConfigResponse;
  e2eImpact?: E2EImpactResponse;
  config?: ProjectConfigResponse;
  gitRefs?: ProjectGitRefsResponse;
  error?: string;
  errors: {
    status?: RuntimeFailure;
    summary?: RuntimeFailure;
    e2eImpact?: RuntimeFailure;
    config?: RuntimeFailure;
    gitRefs?: RuntimeFailure;
  };
}

interface RuntimeFailure {
  message: string;
  status?: number;
}

type LifecycleKind =
  | "loading"
  | "ready"
  | "graph_stale"
  | "graph_missing"
  | "config_missing"
  | "reconcile_pending"
  | "service_error";

interface Lifecycle {
  kind: LifecycleKind;
  label: string;
  detail: string;
  className: string;
  action?: "build" | "update";
}

interface Notice {
  kind: "success" | "error" | "info";
  message: string;
}

const CLOSED_BACKLOG_STATUSES = new Set(["FIXED", "CLOSED", "DONE", "RESOLVED", "CANCELLED"]);
const DEFAULT_BOOTSTRAP_EXCLUDES = ["node_modules", "dist", "build", ".expo", ".next", "coverage"];

export default function ProjectConsoleView({
  projects,
  currentProjectId,
  initialWorkspacePath,
  loading,
  onOpenProject,
  onOpenAiConfig,
  onRefresh,
}: Props) {
  const [runtime, setRuntime] = useState<Record<string, ProjectRuntime>>({});
  const [runtimeLoading, setRuntimeLoading] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const [workspacePath, setWorkspacePath] = useState(initialWorkspacePath ?? "");
  const [projectName, setProjectName] = useState("");
  const [bootstrapExcludePaths, setBootstrapExcludePaths] = useState(DEFAULT_BOOTSTRAP_EXCLUDES.join("\n"));
  const [excludeReviewConfirmed, setExcludeReviewConfirmed] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [actionState, setActionState] = useState<{ key: string; label: string } | null>(null);
  const [actionStartedAt, setActionStartedAt] = useState<number | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const workspacePathInputRef = useRef<HTMLInputElement | null>(null);
  const appliedInitialWorkspacePathRef = useRef(initialWorkspacePath ?? "");
  const projectKey = useMemo(() => projects.map((p) => p.project_id).join("\u0000"), [projects]);

  useEffect(() => {
    const next = (initialWorkspacePath ?? "").trim();
    if (next && appliedInitialWorkspacePathRef.current !== next) {
      appliedInitialWorkspacePathRef.current = next;
      setWorkspacePath((prev) => (prev.trim() ? prev : next));
    }
  }, [initialWorkspacePath]);

  useEffect(() => {
    if (projects.length === 0) {
      setRuntime({});
      return;
    }
    const ac = new AbortController();
    setRuntimeLoading(true);
    void loadProjectRuntime(projects, ac.signal)
      .then((rows) => {
        if (ac.signal.aborted) return;
        setRuntime(Object.fromEntries(rows.map((row) => [row.projectId, row])));
      })
      .finally(() => {
        if (!ac.signal.aborted) setRuntimeLoading(false);
      });
    return () => ac.abort();
  }, [projectKey, projects, refreshToken]);

  useEffect(() => {
    if (!actionState) {
      setActionStartedAt(null);
      return;
    }
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    const poller = window.setInterval(() => {
      setRefreshToken((value) => value + 1);
      void onRefresh();
    }, 2000);
    return () => {
      window.clearInterval(timer);
      window.clearInterval(poller);
    };
  }, [actionState, onRefresh]);

  const rows = useMemo(
    () =>
      projects
        .slice()
        .sort((a, b) => {
          if (a.project_id === currentProjectId) return -1;
          if (b.project_id === currentProjectId) return 1;
          return a.project_id.localeCompare(b.project_id);
        }),
    [currentProjectId, projects],
  );
  const currentProjectLabel = projectLabelFor(projects, currentProjectId);

  const stats = useMemo(() => {
    const runtimes = Object.values(runtime);
    return {
      total: projects.length,
      current: runtimes.filter((r) => r.status?.current_state?.graph_stale?.is_stale === false).length,
      stale: runtimes.filter((r) => r.status?.current_state?.graph_stale?.is_stale === true).length,
      missing: projects.filter((p) => lifecycleFor(p, runtime[p.project_id]).kind === "graph_missing").length,
      backlogOpen: runtimes.reduce((sum, r) => sum + countOpenBacklog(r.backlog), 0),
    };
  }, [projects.length, runtime]);

  const refreshRuntime = async () => {
    setRefreshToken((value) => value + 1);
    await onRefresh();
  };

  const handleBootstrap = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const path = workspacePath.trim();
    if (!path) {
      setNotice({ kind: "error", message: "Workspace path is required." });
      return;
    }
    const excludePaths = parseBootstrapExcludePaths(bootstrapExcludePaths);
    if (!excludeReviewConfirmed) {
      setNotice({
        kind: "error",
        message: "Confirm excluded directories before bootstrap so generated or local-only files do not enter the graph.",
      });
      return;
    }
    setActionState({ key: "bootstrap", label: "Bootstrapping" });
    setActionStartedAt(Date.now());
    setNotice({ kind: "info", message: `Bootstrapping project with ${excludePaths.length} excluded path(s)...` });
    try {
      const result = await api.bootstrapProject({
        workspace_path: path,
        project_name: projectName.trim() || undefined,
        scan_depth: 3,
        exclude_patterns: excludePaths,
        config_override: { graph: { exclude_paths: excludePaths } },
      });
      setWorkspacePath("");
      setProjectName("");
      setBootstrapExcludePaths(DEFAULT_BOOTSTRAP_EXCLUDES.join("\n"));
      setExcludeReviewConfirmed(false);
      setNotice({
        kind: "success",
        message: `Bootstrapped ${result.project_id}${result.snapshot_id ? ` · ${result.snapshot_id}` : ""}`,
      });
      await refreshRuntime();
    } catch (error) {
      setNotice({ kind: "error", message: `Bootstrap failed: ${errorMessage(error)}` });
    } finally {
      setActionState(null);
    }
  };

  const handleChooseDirectory = async () => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 9000);
    setActionState({ key: "choose-directory", label: "Choosing directory" });
    setNotice({
      kind: "info",
      message: "Opening the local directory picker. You can paste the workspace path manually and continue.",
    });
    window.setTimeout(() => workspacePathInputRef.current?.focus(), 0);
    try {
      const result = await api.chooseLocalDirectory({
        initial_path: workspacePath.trim() || undefined,
        title: "Import project directory",
        timeout_seconds: 8,
      }, controller.signal);
      if (result.selected && result.path) {
        setWorkspacePath(result.path);
        setNotice({ kind: "success", message: "Directory selected." });
      } else if (result.error) {
        setNotice({
          kind: "info",
          message: `Directory picker unavailable. Paste the path manually. ${result.error}`,
        });
      } else {
        setNotice({ kind: "info", message: "No directory selected. Paste a path or choose again." });
      }
    } catch (error) {
      const timedOut = error instanceof DOMException && error.name === "AbortError";
      setNotice({
        kind: "info",
        message: timedOut
          ? "Directory picker did not respond. Paste the path manually, then click Bootstrap."
          : `Directory picker unavailable. Paste the path manually. ${errorMessage(error)}`,
      });
    } finally {
      window.clearTimeout(timeout);
      setActionState(null);
      window.setTimeout(() => workspacePathInputRef.current?.focus(), 0);
    }
  };

  const handleBuildGraph = async (project: ProjectListItem) => {
    const key = actionKey(project.project_id, "build");
    setActionState({ key, label: "Building graph" });
    setActionStartedAt(Date.now());
    setNotice({ kind: "info", message: `Building graph for ${project.project_id}...` });
    try {
      const result = await api.fullReconcileFor(project.project_id, {
        run_id: `dashboard-full-${project.project_id}-${Date.now()}`,
        actor: "dashboard",
        activate: true,
        semantic_enrich: true,
        semantic_use_ai: false,
        enqueue_stale: false,
        semantic_skip_completed: true,
        notes_extra: { source: "dashboard_project_console", action: "build_graph" },
      });
      setNotice({
        kind: "success",
        message: `Graph built for ${project.project_id}${result.snapshot_id ? ` · ${result.snapshot_id}` : ""}`,
      });
      await refreshRuntime();
    } catch (error) {
      setNotice({ kind: "error", message: `Build graph failed: ${errorMessage(error)}` });
    } finally {
      setActionState(null);
    }
  };

  const handleUpdateGraph = async (project: ProjectListItem, row?: ProjectRuntime) => {
    const key = actionKey(project.project_id, "update");
    const targetCommit = targetCommitFor(row);
    if (!targetCommit) {
      setNotice({ kind: "error", message: `No target commit available for ${project.project_id}.` });
      return;
    }
    setActionState({ key, label: "Updating graph" });
    setActionStartedAt(Date.now());
    setNotice({ kind: "info", message: `Updating graph for ${project.project_id}...` });
    try {
      const graphStale = row?.status?.current_state?.graph_stale;
      const result = await api.materializePendingScopeFor(project.project_id, {
        target_commit_sha: targetCommit,
        parent_commit_sha: graphStale?.active_graph_commit || row?.status?.graph_snapshot_commit || "",
        run_id: `dashboard-scope-${project.project_id}-${shortCommit(targetCommit)}-${Date.now()}`,
        actor: "dashboard",
        activate: true,
        semantic_enrich: true,
        semantic_use_ai: false,
        enqueue_stale: false,
        semantic_skip_completed: true,
        notes_extra: { source: "dashboard_project_console", action: "update_graph" },
      });
      setNotice({
        kind: "success",
        message: `Graph updated for ${project.project_id}${result.snapshot_id ? ` · ${result.snapshot_id}` : ""}`,
      });
      await refreshRuntime();
    } catch (error) {
      setNotice({ kind: "error", message: `Update graph failed: ${errorMessage(error)}` });
    } finally {
      setActionState(null);
    }
  };

  const handleSelectRef = async (project: ProjectListItem, selectedRef: string) => {
    const ref = selectedRef.trim();
    if (!ref) return;
    const key = actionKey(project.project_id, "ref");
    setActionState({ key, label: "Saving ref" });
    setActionStartedAt(Date.now());
    setNotice({ kind: "info", message: `Saving ref for ${project.project_id}...` });
    try {
      const result = await api.selectGitRefFor(project.project_id, {
        selected_ref: ref,
        actor: "dashboard",
      });
      setNotice({
        kind: "success",
        message: `Selected ${result.selected_ref || ref} for ${project.project_id}`,
      });
      await refreshRuntime();
    } catch (error) {
      setNotice({ kind: "error", message: `Select ref failed: ${errorMessage(error)}` });
    } finally {
      setActionState(null);
    }
  };

  return (
    <div className="view project-console">
      <div className="view-head">
        <h2 className="view-title">Projects</h2>
        <span className="view-subtitle">
          local plugin console · {rows.length} registered · current{" "}
          <span>{currentProjectLabel}</span>
          {currentProjectLabel !== currentProjectId ? (
            <>
              {" "}
              <span className="mono">({currentProjectId})</span>
            </>
          ) : null}
        </span>
      </div>

      <div className="score-grid project-console-score-grid">
        <Kpi label="Registered" value={stats.total} tone="blue" />
        <Kpi label="Graph current" value={stats.current} tone="green" />
        <Kpi label="Graph stale" value={stats.stale} tone={stats.stale > 0 ? "amber" : "neutral"} />
        <Kpi label="Graph missing" value={stats.missing} tone={stats.missing > 0 ? "red" : "neutral"} />
        <Kpi label="Open backlog" value={stats.backlogOpen} tone={stats.backlogOpen > 0 ? "amber" : "neutral"} />
      </div>

      <form className="project-bootstrap card" data-testid="project-import-form" onSubmit={handleBootstrap}>
        <div className="project-bootstrap-fields">
          <label>
            <span>Workspace path</span>
            <input
              data-testid="project-import-workspace-path"
              ref={workspacePathInputRef}
              value={workspacePath}
              onChange={(event) => setWorkspacePath(event.target.value)}
              placeholder="C:\\path\\to\\project"
            />
          </label>
          <button
            type="button"
            className="action-btn project-import-directory-btn"
            data-testid="project-import-directory"
            disabled={actionState?.key === "choose-directory"}
            onClick={handleChooseDirectory}
          >
            {actionState?.key === "choose-directory" ? "Choosing..." : "Choose directory"}
          </button>
          <label>
            <span>Project name</span>
            <input
              value={projectName}
              onChange={(event) => setProjectName(event.target.value)}
              placeholder="optional"
            />
          </label>
          <label className="project-bootstrap-excludes">
            <span>Exclude paths before graph build</span>
            <textarea
              data-testid="project-import-exclude-paths"
              value={bootstrapExcludePaths}
              onChange={(event) => {
                setBootstrapExcludePaths(event.target.value);
                setExcludeReviewConfirmed(false);
              }}
              placeholder={"node_modules\ndist\nbuild\ncoverage"}
              rows={4}
            />
            <small>
              One path prefix per line. Add project-specific generated, vendored, nested, or scratch directories
              such as <code>node</code>, <code>vendor</code>, generated clients, fixture clones, or docs scratch roots.
            </small>
          </label>
          <label className="project-bootstrap-confirm">
            <input
              type="checkbox"
              data-testid="project-import-exclude-confirm"
              checked={excludeReviewConfirmed}
              onChange={(event) => setExcludeReviewConfirmed(event.target.checked)}
            />
            <span>I reviewed which directories should not be included in the graph.</span>
          </label>
          <button
            className="action-btn action-btn-primary"
            data-testid="project-import-bootstrap"
            disabled={actionState?.key === "bootstrap"}
          >
            {actionState?.key === "bootstrap" ? "Bootstrapping..." : "Bootstrap"}
          </button>
        </div>
        {notice ? (
          <div className={`project-console-notice ${notice.kind}`}>
            {notice.message}
          </div>
        ) : null}
        {actionState ? (
          <div className="project-console-progress" role="status">
            <span className="project-console-progress-dot" />
            <strong>{actionState.label}</strong>
            <span>{elapsedLabel(actionStartedAt, nowMs)}</span>
            <span>polling registry status</span>
          </div>
        ) : null}
      </form>

      <div className="section">
        <div className="section-head">
          Project Registry{" "}
          <span className="head-hint">
            {runtimeLoading || loading ? "refreshing" : "live"}
          </span>
        </div>
        <div className="project-console-guide">
          <span><strong>Build graph</strong> runs a full local scan for a new or broken graph.</span>
          <span><strong>Update graph</strong> catches an existing graph up to the selected ref/HEAD without live AI calls.</span>
        </div>
        <div className="card project-console-table-card">
          <div className="project-console-table-wrap">
            <table className="table project-console-table">
              <colgroup>
                <col className="project-console-col-project" />
                <col className="project-console-col-ref" />
                <col className="project-console-col-graph" />
                <col className="project-console-col-snapshot" />
                <col className="project-console-col-scale" />
                <col className="project-console-col-work" />
                <col className="project-console-col-ai" />
                <col className="project-console-col-workspace" />
                <col className="project-console-col-actions" />
              </colgroup>
              <thead>
                <tr>
                  <th>Project</th>
                  <th>Ref</th>
                  <th>Graph</th>
                  <th>Snapshot</th>
                  <th>Scale</th>
                  <th>Work</th>
                  <th>AI</th>
                  <th>Workspace</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((project) => {
                  const row = runtime[project.project_id];
                  const selected = project.project_id === currentProjectId;
                  return (
                    <ProjectRow
                      key={project.project_id}
                      project={project}
                      runtime={row}
                      selected={selected}
                      lifecycle={lifecycleFor(project, row)}
                      busyLabel={busyLabelFor(project.project_id, actionState)}
                      localElapsed={busyLabelFor(project.project_id, actionState) ? elapsedLabel(actionStartedAt, nowMs) : undefined}
                      onOpenProject={onOpenProject}
                      onOpenAiConfig={onOpenAiConfig}
                      onBuildGraph={() => handleBuildGraph(project)}
                      onUpdateGraph={() => handleUpdateGraph(project, row)}
                      onSelectRef={(selectedRef) => handleSelectRef(project, selectedRef)}
                    />
                  );
                })}
                {rows.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="empty" style={{ padding: 16 }}>
                      No registered projects.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}

function ProjectRow({
  project,
  runtime,
  selected,
  lifecycle,
  busyLabel,
  localElapsed,
  onOpenProject,
  onOpenAiConfig,
  onBuildGraph,
  onUpdateGraph,
  onSelectRef,
}: {
  project: ProjectListItem;
  runtime?: ProjectRuntime;
  selected: boolean;
  lifecycle: Lifecycle;
  busyLabel?: string;
  localElapsed?: string;
  onOpenProject(projectId: string): void;
  onOpenAiConfig(): void;
  onBuildGraph(): void;
  onUpdateGraph(): void;
  onSelectRef(selectedRef: string): void;
}) {
  const summary = runtime?.summary;
  const ops = runtime?.ops;
  const backlogOpen = countOpenBacklog(runtime?.backlog);
  const e2eRequired = runtime?.e2eImpact?.summary?.required;
  const aiRoute = runtime?.aiConfig?.semantic;
  const actionBusy = Boolean(busyLabel);
  const actionDisabled = actionBusy || lifecycle.kind === "config_missing" || lifecycle.kind === "service_error";
  const progress = project.bootstrap_progress;
  const progressLabel = progressLabelFor(progress, busyLabel, localElapsed);

  return (
    <tr className={selected ? "project-console-selected" : ""}>
      <td>
        <div className="project-console-name">
          <span className="cell-strong">{project.name || project.project_id}</span>
          {selected ? <span className="project-console-current">current</span> : null}
        </div>
        <div className="cell-mono-id">{project.project_id}</div>
        <div className="project-console-sub">
          {project.status || (project.initialized ? "initialized" : "registered")}
        </div>
        {progressLabel ? <div className="project-console-progress-row">{progressLabel}</div> : null}
      </td>
      <td>
        <ProjectRefControl
          project={project}
          refs={runtime?.gitRefs}
          disabled={actionBusy}
          onSelect={onSelectRef}
        />
      </td>
      <td>
        <span className={`status-badge ${lifecycle.className}`}>{lifecycle.label}</span>
        <div className="project-console-sub">{lifecycle.detail}</div>
        {runtime?.status?.pending_scope_reconcile_count ? (
          <div className="project-console-sub mono">
            pending scope {runtime.status.pending_scope_reconcile_count}
          </div>
        ) : null}
        {runtime?.error ? <div className="project-console-error">{runtime.error}</div> : null}
      </td>
      <td>
        <div className="mono">{runtime?.status?.active_snapshot_id || project.active_snapshot_id || "—"}</div>
        <div className="project-console-sub mono">
          {shortCommit(runtime?.status?.graph_snapshot_commit || summary?.commit_sha || "")}
        </div>
      </td>
      <td>
        <MetricLine label="nodes" value={summary?.counts.nodes ?? project.node_count} />
        <MetricLine label="files" value={summary?.counts.files} />
        <MetricLine label="features" value={summary?.counts.features} />
      </td>
      <td>
        <MetricLine label="ops" value={ops?.count} />
        <MetricLine label="backlog" value={backlogOpen} />
        <MetricLine label="review" value={ops?.summary?.feedback_queue?.visible_group_count} />
        <MetricLine label="e2e req" value={e2eRequired} />
      </td>
      <td>
        <div>{formatRoute(aiRoute)}</div>
        <div className="project-console-sub">
          {runtime?.aiConfig?.read_only ? "read-only" : runtime?.aiConfig ? "configured" : "—"}
        </div>
      </td>
      <td>
        <span className="project-console-workspace mono" title={project.workspace_path || ""}>
          {project.workspace_path || "—"}
        </span>
      </td>
      <td>
        <div className="project-console-actions">
          <button
            className="action-btn"
            disabled={actionBusy}
            onClick={() => onOpenProject(project.project_id)}
            title="Open this project in the dashboard"
          >
            Open
          </button>
          {lifecycle.action === "build" ? (
            <button
              className="action-btn"
              disabled={actionDisabled}
              onClick={onBuildGraph}
              title="Run full graph reconcile without AI enrichment"
            >
              Build graph
            </button>
          ) : null}
          {lifecycle.action === "update" ? (
            <button
              className="action-btn"
              disabled={actionDisabled}
              onClick={onUpdateGraph}
              title="Run scope reconcile without AI enrichment"
            >
              Update graph
            </button>
          ) : null}
          <button
            className="action-btn"
            disabled={!selected || actionBusy}
            onClick={onOpenAiConfig}
            title={selected ? "Open AI configuration" : "Open the project first"}
          >
            AI config
          </button>
          {busyLabel ? (
            <span className="project-console-busy">
              {busyLabel}
              {localElapsed ? ` · ${localElapsed}` : ""}
            </span>
          ) : null}
        </div>
      </td>
    </tr>
  );
}

function MetricLine({ label, value }: { label: string; value?: number }) {
  return (
    <div className="project-console-metric">
      <span>{label}</span>
      <span className="mono">{value ?? "—"}</span>
    </div>
  );
}

function ProjectRefControl({
  project,
  refs,
  disabled,
  onSelect,
}: {
  project: ProjectListItem;
  refs?: ProjectGitRefsResponse;
  disabled: boolean;
  onSelect(selectedRef: string): void;
}) {
  const selected = refs?.selected_ref || project.selected_ref || refs?.current_branch || "";
  const branches = refs?.branches ?? [];
  const options = Array.from(new Set([selected, refs?.current_branch, ...branches].filter(Boolean) as string[]));
  const head = shortCommit(refs?.head_commit || "");

  if (!refs?.is_git_repo) {
    return (
      <div>
        <div className="project-console-sub">no git refs</div>
        <div className="project-console-sub mono">{project.selected_ref || "—"}</div>
      </div>
    );
  }

  return (
    <div className="project-ref-control">
      <select
        value={selected}
        disabled={disabled || options.length === 0}
        onChange={(event) => onSelect(event.target.value)}
        title="Select project ref for dashboard graph actions"
      >
        {options.map((ref) => (
          <option key={ref} value={ref}>
            {ref}
          </option>
        ))}
      </select>
      <div className="project-console-sub mono">
        {refs.current_branch ? `worktree ${refs.current_branch}` : head}
      </div>
    </div>
  );
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

async function loadProjectRuntime(projects: ProjectListItem[], signal: AbortSignal): Promise<ProjectRuntime[]> {
  return Promise.all(projects.map((project) => loadOneProjectRuntime(project, signal)));
}

async function loadOneProjectRuntime(project: ProjectListItem, signal: AbortSignal): Promise<ProjectRuntime> {
  const projectId = project.project_id;
  const [status, summary, ops, backlog, aiConfig, e2eImpact, config, gitRefs] = await Promise.allSettled([
    api.statusFor(projectId, signal),
    api.activeSummaryFor(projectId, signal),
    api.operationsQueueFor(projectId, signal),
    api.backlogFor(projectId, signal),
    api.aiConfigFor(projectId, signal),
    api.e2eImpactFor(projectId, "active", signal),
    api.projectConfigFor(projectId, signal),
    api.gitRefsFor(projectId, signal),
  ]);
  const statusError = failure(status);
  const summaryError = failure(summary);
  const e2eImpactError = failure(e2eImpact);
  const configError = failure(config);
  const gitRefsError = failure(gitRefs);
  return {
    projectId,
    status: settledValue(status),
    summary: settledValue(summary),
    ops: settledValue(ops),
    backlog: settledValue(backlog),
    aiConfig: settledValue(aiConfig),
    e2eImpact: settledValue(e2eImpact),
    config: settledValue(config),
    gitRefs: settledValue(gitRefs),
    error: firstError(statusError, summaryError),
    errors: {
      status: statusError,
      summary: summaryError,
      e2eImpact: e2eImpactError,
      config: configError,
      gitRefs: gitRefsError,
    },
  };
}

function settledValue<T>(result: PromiseSettledResult<T>): T | undefined {
  return result.status === "fulfilled" ? result.value : undefined;
}

function failure(result: PromiseSettledResult<unknown>): RuntimeFailure | undefined {
  if (result.status === "fulfilled") return undefined;
  const reason = result.reason;
  if (reason instanceof ApiError) return { message: `HTTP ${reason.status}`, status: reason.status };
  return { message: reason instanceof Error ? reason.message : "error" };
}

function firstError(...failures: Array<RuntimeFailure | undefined>): string | undefined {
  return failures.find(Boolean)?.message;
}

function countOpenBacklog(backlog?: BacklogResponse): number {
  return (
    backlog?.bugs?.filter((bug) => {
      const status = String(bug.status || "OPEN").toUpperCase();
      return !CLOSED_BACKLOG_STATUSES.has(status);
    }).length ?? 0
  );
}

function projectLabelFor(projects: ProjectListItem[], projectId: string): string {
  const project = projects.find((p) => p.project_id === projectId);
  return project?.name?.trim() || projectId;
}

function shortCommit(commit: string): string {
  if (!commit) return "—";
  return commit.length > 10 ? commit.slice(0, 7) : commit;
}

function formatRoute(route?: { provider?: string; model?: string } | null): string {
  if (!route) return "—";
  const provider = route.provider || "default";
  const model = route.model || "default";
  return `${provider} / ${model}`;
}

function lifecycleFor(project: ProjectListItem, runtime?: ProjectRuntime): Lifecycle {
  if (!runtime) {
    return {
      kind: "loading",
      label: "loading",
      detail: "checking",
      className: "status-unknown",
    };
  }
  const pending = runtime.status?.pending_scope_reconcile_count ?? 0;
  if (pending > 0) {
    return {
      kind: "reconcile_pending",
      label: "pending",
      detail: `${pending} scope row${pending === 1 ? "" : "s"}`,
      className: "status-running",
      action: "update",
    };
  }
  const graphStale = runtime.status?.current_state?.graph_stale;
  if (graphStale?.is_stale) {
    return {
      kind: "graph_stale",
      label: "stale",
      detail: shortCommit(graphStale.active_graph_commit || "") + " -> " + shortCommit(graphStale.head_commit || ""),
      className: "status-pending",
      action: "update",
    };
  }
  const hasGraph = Boolean(runtime.status?.active_snapshot_id || runtime.summary || project.active_snapshot_id);
  if (hasGraph && !runtime.errors.status && !runtime.errors.summary) {
    return {
      kind: "ready",
      label: "ready",
      detail: runtime.summary?.snapshot_kind || "active graph",
      className: "status-complete",
    };
  }
  const configMissing = Boolean(runtime.errors.config || runtime.aiConfig?.project_config_error);
  if (configMissing) {
    return {
      kind: "config_missing",
      label: "config missing",
      detail: runtime.errors.config?.message || runtime.aiConfig?.project_config_error || "no project config",
      className: "status-failed",
    };
  }
  if (runtime.errors.status?.status === 404 || runtime.errors.summary?.status === 404) {
    return {
      kind: "graph_missing",
      label: "graph missing",
      detail: "needs full reconcile",
      className: "status-failed",
      action: "build",
    };
  }
  if (runtime.errors.status || runtime.errors.summary) {
    return {
      kind: "service_error",
      label: "service error",
      detail: runtime.error || "request failed",
      className: "status-failed",
    };
  }
  return {
    kind: "graph_missing",
    label: "graph missing",
    detail: "needs full reconcile",
    className: "status-failed",
    action: "build",
  };
}

function targetCommitFor(runtime?: ProjectRuntime): string {
  const stale = runtime?.status?.current_state?.graph_stale;
  if (stale?.head_commit) return stale.head_commit;
  const pending = runtime?.status?.pending_scope_reconcile?.[0] as
    | { commit_sha?: string; target_commit_sha?: string }
    | undefined;
  return pending?.commit_sha || pending?.target_commit_sha || runtime?.status?.graph_snapshot_commit || "";
}

function actionKey(projectId: string, action: "build" | "update" | "ref"): string {
  return `${action}:${projectId}`;
}

function busyLabelFor(projectId: string, state: { key: string; label: string } | null): string | undefined {
  if (!state) return undefined;
  return state.key.endsWith(`:${projectId}`) || state.key === "bootstrap" ? state.label : undefined;
}

function elapsedLabel(startedAt: number | null, nowMs: number): string {
  if (!startedAt) return "elapsed 0s";
  const seconds = Math.max(0, Math.floor((nowMs - startedAt) / 1000));
  if (seconds < 60) return `elapsed ${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `elapsed ${minutes}m ${rest}s`;
}

function progressLabelFor(
  progress: ProjectOperationProgress | undefined,
  localLabel?: string,
  localElapsed?: string,
): string | undefined {
  if (localLabel) {
    return `${localLabel}${localElapsed ? ` · ${localElapsed}` : ""}`;
  }
  if (!progress?.status) return undefined;
  const phase = progress.phase ? ` · ${progress.phase}` : "";
  const message = progress.message ? ` · ${progress.message}` : "";
  const elapsed = typeof progress.elapsed_seconds === "number" ? ` · elapsed ${formatSeconds(progress.elapsed_seconds)}` : "";
  if (progress.status === "running") {
    return `${operationLabel(progress.operation)} running${phase}${elapsed}${message}`;
  }
  if (progress.status === "failed") {
    return `${operationLabel(progress.operation)} failed${phase}${message}`;
  }
  if (progress.status === "succeeded") {
    return `${operationLabel(progress.operation)} complete${phase}${message}`;
  }
  return `${operationLabel(progress.operation)} ${progress.status}${phase}${message}`;
}

function operationLabel(operation?: string): string {
  if (operation === "bootstrap") return "Bootstrap";
  if (operation === "build_graph") return "Build graph";
  if (operation === "update_graph") return "Update graph";
  return "Project operation";
}

function formatSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}m ${rest}s`;
}

function parseBootstrapExcludePaths(value: string): string[] {
  const seen = new Set<string>();
  for (const raw of value.split(/[\n,]+/)) {
    const normalized = raw.replace(/\\/g, "/").trim().replace(/^\/+|\/+$/g, "");
    if (!normalized || normalized === "." || seen.has(normalized)) continue;
    seen.add(normalized);
  }
  return Array.from(seen);
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}${error.body ? ` ${error.body}` : ""}`;
  return error instanceof Error ? error.message : String(error);
}
