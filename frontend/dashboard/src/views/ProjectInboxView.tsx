import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../lib/api";
import type { ObserverCommand, ProjectInboxItem, ProjectInboxResponse, RawRequirement } from "../types";

interface Props {
  projectId: string;
}

const LANE_ORDER: Array<keyof ProjectInboxResponse["lanes"]> = [
  "raw_inbox",
  "needs_confirmation",
  "ready_backlog",
  "in_progress",
  "review_needed",
  "done",
];

const LANE_LABELS: Record<keyof ProjectInboxResponse["lanes"], string> = {
  raw_inbox: "Raw Inbox",
  needs_confirmation: "Needs Confirmation",
  ready_backlog: "Ready Backlog",
  in_progress: "In Progress",
  review_needed: "Review Needed",
  done: "Done",
};

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return `${error.message} ${error.body}`;
  return error instanceof Error ? error.message : String(error);
}

function isRawRequirement(item: ProjectInboxItem): item is RawRequirement {
  return "raw_id" in item;
}

function itemKey(item: ProjectInboxItem): string {
  return isRawRequirement(item) ? item.raw_id : item.bug_id;
}

function itemTitle(item: ProjectInboxItem): string {
  return isRawRequirement(item) ? item.raw_text : item.title || item.bug_id;
}

function itemMeta(item: ProjectInboxItem): string {
  if (isRawRequirement(item)) return item.raw_id;
  return [item.priority, item.status, item.runtime_state].filter(Boolean).join(" / ");
}

function itemTimestamp(item: ProjectInboxItem): string {
  const value = isRawRequirement(item) ? item.created_at : item.updated_at || item.created_at || "";
  if (!value) return "";
  return new Date(value).toLocaleString();
}

function commandForRaw(row: RawRequirement, commands: ObserverCommand[]): ObserverCommand | null {
  return (
    commands.find(
      (command) =>
        command.command_type === "analyze_requirements" &&
        String(command.payload?.raw_id ?? "") === row.raw_id,
    ) ?? null
  );
}

function commandLabel(command: ObserverCommand | null): string {
  if (!command) return "Not queued";
  if (command.status === "queued" || command.status === "notified") return "Queued";
  if (command.status === "claimed" || command.status === "running") return "Running";
  if (command.status === "completed") return "Completed";
  if (command.status === "failed") return "Failed";
  if (command.status === "cancelled") return "Cancelled";
  return command.status;
}

function commandTone(command: ObserverCommand | null): string {
  if (!command) return "neutral";
  if (command.status === "completed") return "complete";
  if (command.status === "failed" || command.status === "cancelled") return "failed";
  if (command.status === "claimed" || command.status === "running") return "running";
  return "queued";
}

export default function ProjectInboxView({ projectId }: Props) {
  const [inbox, setInbox] = useState<ProjectInboxResponse | null>(null);
  const [rawText, setRawText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (signal?: AbortSignal) => {
    const next = await api.projectInboxFor(projectId, signal);
    setInbox(next);
  }, [projectId]);

  useEffect(() => {
    const controller = new AbortController();
    setError("");
    void load(controller.signal).catch((err) => {
      if ((err as { name?: string }).name === "AbortError") return;
      setError(errorMessage(err));
    });
    return () => controller.abort();
  }, [load]);

  const rawCount = inbox?.lanes.raw_inbox.count ?? 0;
  const confirmCount = inbox?.lanes.needs_confirmation.count ?? 0;
  const observerConnected = Boolean(inbox?.observer?.connected);
  const observerLabel = observerConnected
    ? `Observer connected (${inbox?.observer?.connected_count ?? 0})`
    : "Waiting for observer";
  const commandItems = inbox?.observer_commands?.items ?? [];
  const runningCommandCount =
    (inbox?.observer_commands?.counts.claimed ?? 0) + (inbox?.observer_commands?.counts.running ?? 0);
  const queuedCommandCount =
    (inbox?.observer_commands?.counts.queued ?? 0) + (inbox?.observer_commands?.counts.notified ?? 0);
  const totalIntent = useMemo(
    () => LANE_ORDER.reduce((sum, lane) => sum + (inbox?.lanes[lane]?.count ?? 0), 0),
    [inbox],
  );

  const capture = async () => {
    const text = rawText.trim();
    if (!text) return;
    setBusy(true);
    setError("");
    try {
      await api.captureRawRequirementFor(projectId, {
        raw_text: text,
        source: "dashboard_project_inbox",
        actor: "dashboard",
      });
      setRawText("");
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const analyzeRequirement = async (row: RawRequirement) => {
    setBusy(true);
    setError("");
    try {
      await api.enqueueObserverCommandFor(projectId, {
        command_type: "analyze_requirements",
        payload: {
          raw_id: row.raw_id,
          source: "project_inbox",
        },
        created_by: "dashboard",
      });
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const moveToConfirmation = async (row: RawRequirement) => {
    setBusy(true);
    setError("");
    try {
      await api.updateRawRequirementStatusFor(projectId, row.raw_id, {
        status: "needs_confirmation",
      });
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="view project-inbox-view">
      <div className="view-head project-inbox-head">
        <div>
          <h2 className="view-title">Project Inbox</h2>
          <p className="view-subtitle">
            Capture raw requirements first. Promote them only after confirmation.
          </p>
        </div>
        <div className="project-inbox-stats">
          <Kpi label="Raw" value={rawCount} />
          <Kpi label="Confirm" value={confirmCount} />
          <Kpi label="Total" value={totalIntent} />
        </div>
      </div>

      <section className="project-inbox-capture">
        <textarea
          value={rawText}
          onChange={(event) => setRawText(event.target.value)}
          placeholder="Drop the user's exact requirement here. Capture mode stores it as-is; it does not dispatch work."
          rows={4}
        />
        <div className="project-inbox-capture-actions">
          <span>Capture mode: no graph query, no decomposition, no implementation backlog row.</span>
          <button
            type="button"
            className="action-btn action-btn-primary"
            disabled={busy || !rawText.trim()}
            onClick={capture}
          >
            {busy ? "Capturing..." : "Capture Raw Requirement"}
          </button>
        </div>
      </section>

      <section className="project-inbox-observer">
        <span className={`project-inbox-observer-pill ${observerConnected ? "connected" : "waiting"}`}>
          {observerLabel}
        </span>
        <span className="project-inbox-command-count">Queued {queuedCommandCount}</span>
        <span className="project-inbox-command-count">Running {runningCommandCount}</span>
        <span className="project-inbox-command-count">
          Failed {inbox?.observer_commands?.counts.failed ?? 0}
        </span>
      </section>

      {error ? <div className="notice error">{error}</div> : null}

      <div className="project-inbox-lanes">
        {LANE_ORDER.map((lane) => {
          const data = inbox?.lanes[lane];
          const items = data?.items ?? [];
          return (
            <section className="project-inbox-lane" key={lane}>
              <div className="project-inbox-lane-head">
                <h3>{LANE_LABELS[lane]}</h3>
                <span className="pill pill-mono">{data?.count ?? 0}</span>
              </div>
              {items.length ? (
                <div className="project-inbox-items">
                  {items.map((item) => (
                    <ProjectInboxCard
                      key={itemKey(item)}
                      item={item}
                      lane={lane}
                      busy={busy}
                      command={isRawRequirement(item) ? commandForRaw(item, commandItems) : null}
                      onAnalyze={analyzeRequirement}
                      onMoveToConfirmation={moveToConfirmation}
                    />
                  ))}
                </div>
              ) : (
                <div className="project-inbox-empty">
                  {data?.source === "backlog"
                    ? "No backlog rows in this lane."
                    : data?.source === "todo_backlog_join"
                    ? "Backlog join comes next."
                    : "No items in this lane."}
                </div>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}

function ProjectInboxCard({
  item,
  lane,
  busy,
  command,
  onAnalyze,
  onMoveToConfirmation,
}: {
  item: ProjectInboxItem;
  lane: keyof ProjectInboxResponse["lanes"];
  busy: boolean;
  command: ObserverCommand | null;
  onAnalyze: (row: RawRequirement) => void;
  onMoveToConfirmation: (row: RawRequirement) => void;
}) {
  const raw = isRawRequirement(item) ? item : null;
  const preview = !raw && "details_preview" in item ? item.details_preview : "";
  const hasActiveCommand =
    command?.status === "queued" ||
    command?.status === "notified" ||
    command?.status === "claimed" ||
    command?.status === "running";
  return (
    <article className={`project-inbox-item ${raw ? "raw" : "backlog"}`}>
      <div className="project-inbox-item-text">{itemTitle(item)}</div>
      <div className="project-inbox-item-meta">
        <span className="mono">{itemMeta(item)}</span>
        <span>{itemTimestamp(item)}</span>
      </div>
      {preview ? (
        <div className="project-inbox-item-preview">{preview}</div>
      ) : null}
      {raw ? (
        <div className="project-inbox-command-row">
          <span className={`project-inbox-command-status tone-${commandTone(command)}`}>
            {commandLabel(command)}
          </span>
          {command?.error ? <span className="project-inbox-command-error">{command.error}</span> : null}
        </div>
      ) : null}
      {raw ? (
        <div className="project-inbox-card-actions">
          <button
            type="button"
            className="action-btn"
            disabled={busy || hasActiveCommand}
            onClick={() => onAnalyze(raw)}
          >
            {hasActiveCommand ? commandLabel(command) : "AI Analyze"}
          </button>
          {lane === "raw_inbox" ? (
            <button
              type="button"
              className="action-btn"
              disabled={busy}
              onClick={() => onMoveToConfirmation(raw)}
            >
              Move to confirmation
            </button>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

function Kpi({ label, value }: { label: string; value: number }) {
  return (
    <div className="project-inbox-kpi">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
