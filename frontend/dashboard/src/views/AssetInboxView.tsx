import { useMemo, useState, type ReactNode } from "react";
import FileLink from "../components/FileLink";
import type { AssetStatusFilter, AssetTreeSelection } from "../components/TreePanel";
import { api, ApiError } from "../lib/api";
import type {
  AssetInboxBindingCandidate,
  AssetInboxItem,
  AssetInboxMountRelation,
  AssetInboxResponse,
  AssetInboxStatus,
  FileHygieneActionResponse,
  NodeRecord,
} from "../types";

interface Props {
  assetInbox: AssetInboxResponse;
  projectId: string;
  snapshotId: string;
  nodes: NodeRecord[];
  treeSelection: AssetTreeSelection;
  statusFilter: AssetStatusFilter;
  search: string;
  selectedAssetId: string;
  onSelectedAssetIdChange(assetId: string): void;
  onSelectNode?: (nodeId: string) => void;
  workspaceRoot?: string;
}

type AssetGroupId = "ALL" | "doc" | "test" | "config" | "source" | "generated" | "other";
type AttachRole = "doc" | "test" | "config";
type AttachState = "idle" | "writing" | "written_uncommitted" | "error";
type DriftStateName = "not_drifted" | "suspected" | "confirmed" | "resolved" | "waived";
type ActionState = "idle" | "writing" | "written" | "blocked" | "error";
type AssetInspectorTab = "overview" | "relations" | "candidates" | "actions";

interface AttachDraft {
  targetNodeId: string;
  role: AttachRole;
}

interface AttachResult {
  state: AttachState;
  message: string;
}

interface ActionResult {
  state: ActionState;
  message: string;
  followUpId?: string;
  reviewQueueHref?: string;
  requiresCommit?: boolean;
  updateGraphAfterCommit?: boolean;
  sourceControlled?: boolean;
  auditOnly?: boolean;
  operationLabel?: string;
}

interface RemoveConfirmState {
  item: AssetInboxItem;
  relation: RelationView;
  reason: string;
}

interface RelationView extends AssetInboxMountRelation {
  relation_id: string;
  status: "accepted" | "candidate" | "unbound" | "stale_drift" | "impact_pending" | string;
  target_node_id: string;
  precheck?: AssetInboxBindingCandidate["precheck"];
}

interface GroupView {
  id: AssetGroupId;
  label: string;
  count: number;
  itemIds: Set<string>;
  statuses: Record<string, number>;
}

const GROUP_ORDER: AssetGroupId[] = ["doc", "test", "config", "source", "generated", "other", "ALL"];

const GROUP_LABELS: Record<AssetGroupId, string> = {
  ALL: "All assets",
  doc: "Docs",
  test: "Tests",
  config: "Config",
  source: "Source",
  generated: "Generated / Ignored",
  other: "Other",
};

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
  impact_pending: "Impact pending",
  drift_suspected: "Drift suspected",
  drift_confirmed: "Drift confirmed",
  drift_resolved: "Drift resolved",
  drift_waived: "Drift waived",
};

const RELATION_LABELS: Record<string, string> = {
  accepted: "Accepted",
  candidate: "Candidate",
  unbound: "Unbound",
  stale_drift: "Stale / drift",
  impact_pending: "Impact pending",
};

const trustedRelationStatuses = new Set(["accepted", "impact_pending", "stale_drift", "unbound"]);

const DRIFT_LABELS: Record<DriftStateName, string> = {
  not_drifted: "Baseline",
  suspected: "Possible drift",
  confirmed: "Drift confirmed",
  resolved: "Resolved",
  waived: "Waived",
};

const REMOVE_BINDING_RUNTIME_DRIFT_ID = "HN-ASSET-REMOVE-BINDING-RUNTIME-DRIFT-20260525";

const ASSET_INSPECTOR_TABS: Array<{ id: AssetInspectorTab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "relations", label: "Relations" },
  { id: "candidates", label: "Candidates" },
  { id: "actions", label: "Actions" },
];

export default function AssetInboxView({
  assetInbox,
  projectId,
  snapshotId,
  nodes,
  treeSelection,
  statusFilter,
  search,
  selectedAssetId,
  onSelectNode,
  workspaceRoot,
}: Props) {
  const [selectedRelationId, setSelectedRelationId] = useState("");
  const [drafts, setDrafts] = useState<Record<string, AttachDraft>>({});
  const [attachResults, setAttachResults] = useState<Record<string, AttachResult>>({});
  const [actionResults, setActionResults] = useState<Record<string, ActionResult>>({});
  const [removeConfirm, setRemoveConfirm] = useState<RemoveConfirmState | null>(null);

  const items = useMemo(() => (assetInbox.items ?? []).slice().sort(compareAssets), [assetInbox.items]);
  const groups = useMemo(() => buildGroups(assetInbox, items), [assetInbox, items]);
  const visibleItems = useMemo(
    () => filterAssets(items, groups, treeSelection.groupId, treeSelection.bucketId, statusFilter, search),
    [items, groups, search, statusFilter, treeSelection.bucketId, treeSelection.groupId],
  );
  const selectedItem = useMemo(() => {
    if (selectedAssetId) {
      const selected = visibleItems.find((item) => item.asset_id === selectedAssetId);
      if (selected) return selected;
    }
    return visibleItems[0] ?? items.find((item) => item.asset_id === selectedAssetId) ?? null;
  }, [items, selectedAssetId, visibleItems]);
  const selectedRelations = useMemo(() => (selectedItem ? deriveRelations(selectedItem) : []), [selectedItem]);
  const selectedRelation = useMemo(() => {
    if (!selectedRelations.length) return null;
    if (selectedRelationId) {
      const match = selectedRelations.find((relation) => relation.relation_id === selectedRelationId);
      if (match) return match;
    }
    return selectedRelations[0];
  }, [selectedRelationId, selectedRelations]);
  const selectedSummary = useMemo(
    () => (selectedItem ? summarizeRelations(selectedItem, selectedRelations) : null),
    [selectedItem, selectedRelations],
  );
  const nodeOptions = useMemo(
    () =>
      nodes
        .filter((node) => (node.layer || "").toUpperCase() === "L7")
        .slice()
        .sort((a, b) => (a.title || a.node_id).localeCompare(b.title || b.node_id)),
    [nodes],
  );

  const reviewCount = assetInbox.summary?.operator_review_count ?? 0;
  const backlogEligible = assetInbox.summary?.backlog_eligible_count ?? 0;
  const total = assetInbox.summary?.total ?? items.length;
  const acceptedCount = assetInbox.summary?.accepted_count ?? countStatus(assetInbox, "accepted");
  const candidateCount =
    assetInbox.summary?.candidate_count ??
    items.reduce((sum, item) => sum + (item.binding_candidates ?? []).length, 0);

  const updateDraft = (path: string, patch: Partial<AttachDraft>) => {
    setDrafts((current) => {
      const item = items.find((candidate) => candidate.path === path);
      const existing = current[path] ?? {
        targetNodeId: suggestedTargetNodeId(item, nodeOptions),
        role: roleForAsset(item),
      };
      return { ...current, [path]: { ...existing, ...patch } };
    });
  };

  const writeHint = async (item: AssetInboxItem) => {
    const draft = drafts[item.path] ?? {
      targetNodeId: suggestedTargetNodeId(item, nodeOptions),
      role: roleForAsset(item),
    };
    if (!draft.targetNodeId) {
      setAttachResults((current) => ({
        ...current,
        [item.path]: { state: "error", message: "Select a target node first." },
      }));
      return;
    }
    setAttachResults((current) => ({
      ...current,
      [item.path]: { state: "writing", message: "Writing governance hint..." },
    }));
    try {
      const result = await api.attachFileGovernanceHintFor(projectId, snapshotId, {
        path: item.path,
        target_node_id: draft.targetNodeId,
        role: draft.role,
        actor: "dashboard_user",
      });
      setAttachResults((current) => ({
        ...current,
        [item.path]: {
          state: "written_uncommitted",
          message: result.message || "Hint written. Commit this file, then run Update graph.",
        },
      }));
    } catch (error) {
      const msg = error instanceof ApiError ? `${error.message} ${error.body}` : String(error);
      setAttachResults((current) => ({
        ...current,
        [item.path]: { state: "error", message: msg },
      }));
    }
  };

  const recordDriftState = async (item: AssetInboxItem, driftState: DriftStateName) => {
    const key = `${item.asset_id}:drift`;
    setActionResults((current) => ({ ...current, [key]: { state: "writing", message: "Recording drift state..." } }));
    try {
      const result = await api.recordAssetDriftStateFor(projectId, {
        asset_kind: normalizeAssetKind(item.asset_kind),
        asset_path: item.path,
        drift_state: driftState,
        snapshot_id: snapshotId,
        actor: "dashboard_user",
        evidence: { source: "asset_inbox", previous_state: item.drift?.state || "not_drifted" },
      });
      setActionResults((current) => ({
        ...current,
        [key]: {
          state: "written",
          message: `Recorded ${DRIFT_LABELS[driftState]} for ${item.path}.`,
        },
      }));
      if (!result.ok) throw new Error("drift state write was not accepted");
    } catch (error) {
      const msg = error instanceof ApiError ? `${error.message} ${error.body}` : String(error);
      setActionResults((current) => ({ ...current, [key]: { state: "error", message: msg } }));
    }
  };

  const queueResolveDrift = async (item: AssetInboxItem, relation: RelationView | null) => {
    const key = `${item.asset_id}:resolve-drift`;
    setActionResults((current) => ({ ...current, [key]: { state: "writing", message: "Queueing drift proposal..." } }));
    try {
      const result = await api.queueAssetDriftProposalFor(projectId, {
        asset_kind: normalizeAssetKind(item.asset_kind),
        asset_path: item.path,
        snapshot_id: snapshotId,
        node_id: relation?.target_node_id || "",
        mode: "ai_assisted_proposal",
        note: "Queued from Asset Inbox drift controls.",
        actor: "dashboard_user",
      });
      const blocked = !result.ai_available || result.proposal?.status === "blocked";
      setActionResults((current) => ({
        ...current,
        [key]: {
          state: blocked ? "blocked" : "written",
          message: blocked
            ? `Proposal recorded but AI is blocked: ${result.ai_reason}`
            : `Proposal queued with local precheck evidence: ${result.proposal?.proposal_id || "pending"}`,
        },
      }));
    } catch (error) {
      const msg = error instanceof ApiError ? `${error.message} ${error.body}` : String(error);
      setActionResults((current) => ({ ...current, [key]: { state: "error", message: msg } }));
    }
  };

  const proposeRelationAction = async (item: AssetInboxItem, relation: RelationView, action: "attach_to_node" | "remove_binding") => {
    if (action === "remove_binding") {
      setRemoveConfirm({ item, relation, reason: "" });
      return;
    }
    await recordRelationAction(item, relation, action, "Proposal-safe binding add from Asset Inbox.");
  };

  const recordRelationAction = async (
    item: AssetInboxItem,
    relation: RelationView,
    action: "attach_to_node" | "remove_binding",
    reason: string,
  ) => {
    const key = `${relation.relation_id}:${action}`;
    const sourceControlledUnbind = action === "remove_binding" && isSourceControlledUnbindCandidate(item, relation);
    setActionResults((current) => ({
      ...current,
      [key]: {
        state: "writing",
        message: sourceControlledUnbind ? "Writing source-controlled unbind hint..." : "Recording proposal...",
        sourceControlled: sourceControlledUnbind,
        operationLabel: relationOperationLabel(action, sourceControlledUnbind),
      },
    }));
    try {
      if (sourceControlledUnbind) {
        const result = await api.unbindFileGovernanceHintFor(projectId, snapshotId, {
          path: item.path,
          target_node_id: relation.target_node_id,
          role: relationRoleForAction(item, relation),
          reason,
          actor: "dashboard_user",
          dry_run: false,
        });
        setActionResults((current) => ({
          ...current,
          [key]: {
            state: "written",
            message: sourceControlledUnbindMessage(result.message),
            reviewQueueHref: reviewQueueHref(projectId),
            requiresCommit: result.requires_commit ?? result.written_uncommitted ?? result.state === "written_uncommitted",
            updateGraphAfterCommit: result.update_graph_after_commit ?? true,
            sourceControlled: true,
            operationLabel: relationOperationLabel(action, true),
          },
        }));
        return;
      }
      const result = await api.fileHygieneActionFor(projectId, snapshotId, {
        action,
        path: item.path,
        target_node_id: relation.target_node_id,
        role: relationRoleForAction(item, relation),
        reason,
        actor: "dashboard_user",
      });
      setActionResults((current) => ({
        ...current,
        [key]: {
          state: "written",
          message: auditActionMessage(action, result),
          reviewQueueHref: reviewQueueHref(projectId),
          requiresCommit: false,
          updateGraphAfterCommit: false,
          auditOnly: action === "remove_binding",
          operationLabel: relationOperationLabel(action, false),
        },
      }));
    } catch (error) {
      if (sourceControlledUnbind && error instanceof ApiError) {
        await recordAuditOnlyRemoveFallback(item, relation, reason, key, error);
        return;
      }
      const isKnownRemoveDrift = action === "remove_binding" && error instanceof ApiError;
      const msg = isKnownRemoveDrift ? removeBindingRuntimeDriftMessage(error) : error instanceof ApiError ? `${error.message} ${error.body}` : String(error);
      setActionResults((current) => ({
        ...current,
        [key]: {
          state: isKnownRemoveDrift ? "blocked" : "error",
          message: msg,
          followUpId: isKnownRemoveDrift ? REMOVE_BINDING_RUNTIME_DRIFT_ID : undefined,
        },
      }));
    }
  };

  const recordAuditOnlyRemoveFallback = async (
    item: AssetInboxItem,
    relation: RelationView,
    reason: string,
    key: string,
    sourceError: ApiError,
  ) => {
    try {
      const result = await api.fileHygieneActionFor(projectId, snapshotId, {
        action: "remove_binding",
        path: item.path,
        target_node_id: relation.target_node_id,
        role: relationRoleForAction(item, relation),
        reason: `${reason}\n\nSource-controlled unbind endpoint rejected; audit-only fallback used. ${compactErrorDetail(sourceError.body || sourceError.message)}`,
        actor: "dashboard_user",
      });
      setActionResults((current) => ({
        ...current,
        [key]: {
          state: "blocked",
          message: `Audit-only fallback recorded after source-controlled unbind rejected (${sourceError.status}). ${auditActionMessage("remove_binding", result)} Commit/update graph materialization is not guaranteed until ${REMOVE_BINDING_RUNTIME_DRIFT_ID} is resolved.`,
          reviewQueueHref: reviewQueueHref(projectId),
          followUpId: REMOVE_BINDING_RUNTIME_DRIFT_ID,
          requiresCommit: false,
          updateGraphAfterCommit: false,
          auditOnly: true,
          operationLabel: relationOperationLabel("remove_binding", false),
        },
      }));
    } catch (fallbackError) {
      const fallbackDetail = fallbackError instanceof ApiError ? `${fallbackError.message} ${fallbackError.body}` : String(fallbackError);
      setActionResults((current) => ({
        ...current,
        [key]: {
          state: "blocked",
          message: `${removeBindingRuntimeDriftMessage(sourceError)} Audit-only fallback also failed: ${fallbackDetail}`,
          followUpId: REMOVE_BINDING_RUNTIME_DRIFT_ID,
          sourceControlled: true,
          operationLabel: relationOperationLabel("remove_binding", true),
        },
      }));
    }
  };

  const confirmRemoveBinding = async () => {
    if (!removeConfirm) return;
    const reason = removeConfirm.reason.trim();
    if (!reason) return;
    const pending = removeConfirm;
    setRemoveConfirm(null);
    await recordRelationAction(pending.item, pending.relation, "remove_binding", reason);
  };

  return (
    <div className="view asset-browser-view">
      <div className="view-head">
        <h2 className="view-title">Asset Inbox</h2>
        <span className="view-subtitle">
          File governance status - {visibleItems.length} shown - {total} total
        </span>
      </div>

      <section className="asset-relation-browser">
        <main className="asset-detail-panel">
          {selectedItem ? (
            <AssetInspector
              item={selectedItem}
              relations={selectedRelations}
              selectedRelation={selectedRelation}
              selectedRelationId={selectedRelation?.relation_id || ""}
              selectedSummary={selectedSummary}
              nodeOptions={nodeOptions}
              workspaceRoot={workspaceRoot}
              attachResult={attachResults[selectedItem.path] ?? { state: "idle", message: "Not written." }}
              draft={
                drafts[selectedItem.path] ?? {
                  targetNodeId: suggestedTargetNodeId(selectedItem, nodeOptions),
                  role: roleForAsset(selectedItem),
                }
              }
              snapshotId={snapshotId}
              actionResults={actionResults}
              projectId={projectId}
              impactScopePolicy={assetInbox.impact_scope_policy || "accepted_bindings_only"}
              metrics={{
                reviewCount,
                backlogEligible,
                candidateCount,
                acceptedCount,
                total,
                visible: visibleItems.length,
              }}
              onSelectNode={onSelectNode}
              onSelectRelation={setSelectedRelationId}
              onUpdateDraft={(patch) => updateDraft(selectedItem.path, patch)}
              onWriteHint={() => writeHint(selectedItem)}
              onPropose={proposeRelationAction}
              onDriftStateChange={(driftState) => recordDriftState(selectedItem, driftState)}
              onResolveDrift={() => queueResolveDrift(selectedItem, selectedRelation)}
            />
          ) : (
            <div className="asset-browser-empty asset-browser-empty-large">
              No assets are available in this snapshot.
            </div>
          )}
        </main>
      </section>

      {removeConfirm ? (
        <RemoveBindingDialog
          state={removeConfirm}
          projectId={projectId}
          onReasonChange={(reason) => setRemoveConfirm((current) => current ? { ...current, reason } : current)}
          onCancel={() => setRemoveConfirm(null)}
          onConfirm={confirmRemoveBinding}
        />
      ) : null}
    </div>
  );
}

function AssetInspector(props: {
  item: AssetInboxItem;
  relations: RelationView[];
  selectedRelation: RelationView | null;
  selectedRelationId: string;
  selectedSummary: ReturnType<typeof summarizeRelations> | null;
  nodeOptions: NodeRecord[];
  workspaceRoot?: string;
  attachResult: AttachResult;
  draft: AttachDraft;
  snapshotId: string;
  actionResults: Record<string, ActionResult>;
  projectId: string;
  impactScopePolicy: string;
  metrics: {
    reviewCount: number;
    backlogEligible: number;
    candidateCount: number;
    acceptedCount: number;
    total: number;
    visible: number;
  };
  onSelectNode?: (nodeId: string) => void;
  onSelectRelation(relationId: string): void;
  onUpdateDraft(patch: Partial<AttachDraft>): void;
  onWriteHint(): void;
  onPropose(item: AssetInboxItem, relation: RelationView, action: "attach_to_node" | "remove_binding"): void;
  onDriftStateChange(state: DriftStateName): void;
  onResolveDrift(): void;
}) {
  const [tab, setTab] = useState<AssetInspectorTab>("overview");
  const trustedRelations = props.relations.filter((relation) => trustedRelationStatuses.has(relation.status));
  const connected = trustedRelations.filter((relation) => relation.target_node_id);
  const candidates = props.relations.filter((relation) => relation.status === "candidate");
  const tone = assetTone(props.item);
  const selectedTrustedRelation = props.selectedRelation && props.selectedRelation.status !== "candidate" ? props.selectedRelation : null;
  return (
    <section className="asset-inspector" aria-label="Asset Inspector">
      <header className="inspector-head asset-inspector-head">
        <div className="inspector-row">
          <span className="pill pill-mono">{labelForKind(props.item.asset_kind)}</span>
          <span className={`status-badge ${assetStatusClass(props.item.asset_status)}`}>
            {STATUS_LABELS[props.item.asset_status] ?? props.item.asset_status}
          </span>
          <span className="pill pill-mono">{props.item.language || "language n/a"}</span>
          <span className="pill pill-mono">{formatBytes(props.item.size_bytes)}</span>
        </div>
        <div className="inspector-title asset-inspector-title">
          <FileLink path={props.item.path} workspaceRoot={props.workspaceRoot} />
        </div>
        <div className="inspector-mono-line">{props.item.path}</div>
        <div className="inspector-head-state-row asset-inspector-state-row">
          <div className={`sem-state-row tone-${assetInspectorTone(tone)}`}>
            <span className={`asset-state-dot tone-${tone}`} />
            <span>{relationSummaryLabel(props.selectedSummary)}</span>
            <span className="head-hint">{driftStateLabel(props.item)}</span>
          </div>
          <span className="asset-inspector-counts">
            {props.metrics.visible}/{props.metrics.total} shown
          </span>
        </div>
      </header>
      <nav className="inspector-tabs asset-inspector-tabs" role="tablist">
        {ASSET_INSPECTOR_TABS.map((candidate) => (
          <button
            key={candidate.id}
            type="button"
            role="tab"
            aria-selected={tab === candidate.id}
            className={`inspector-tab${tab === candidate.id ? " active" : ""}`}
            onClick={() => setTab(candidate.id)}
          >
            {candidate.label}
          </button>
        ))}
      </nav>
      <div className="inspector-body asset-inspector-body scrollbar-thin">
        {tab === "overview" ? (
          <div className="asset-inspector-grid">
            <DetailBlock title="File">
              <div className="asset-policy-lines">
                <span>
                  Path: <FileLink path={props.item.path} workspaceRoot={props.workspaceRoot} />
                </span>
                <span>Kind: {labelForKind(props.item.asset_kind)}</span>
                <span>Status: {STATUS_LABELS[props.item.asset_status] ?? props.item.asset_status}</span>
                <span>Relations: {relationSummaryLabel(props.selectedSummary)}</span>
                <span>Impact scope: {props.impactScopePolicy}</span>
              </div>
            </DetailBlock>
            <div className="asset-meta-grid asset-meta-grid-compact">
              <Meta label="Hash" value={props.item.file_hash || props.item.sha256 || "n/a"} mono />
              <Meta label="Scan" value={props.item.scan_status || "n/a"} />
              <Meta label="Graph" value={props.item.graph_status || "n/a"} />
              <Meta label="Risk" value={props.item.risk || "unknown"} />
              <Meta label="Size" value={formatBytes(props.item.size_bytes)} />
              <Meta label="Binding" value={props.item.binding_status || relationSummaryLabel(props.selectedSummary)} />
              <Meta label="Drift" value={driftStateLabel(props.item)} />
            </div>
          </div>
        ) : null}

        {tab === "relations" ? (
          <div className="asset-inspector-tab-stack">
            <RelationPanel
              item={props.item}
              relations={trustedRelations}
              selectedRelationId={props.selectedRelationId}
              actionResults={props.actionResults}
              onSelect={props.onSelectRelation}
              onSelectNode={props.onSelectNode}
              onPropose={props.onPropose}
              projectId={props.projectId}
            />
            <SelectedRelationOperation
              item={props.item}
              relation={selectedTrustedRelation}
              result={selectedTrustedRelation ? relationActionResult(props.actionResults, selectedTrustedRelation) : null}
              projectId={props.projectId}
              onSelectNode={props.onSelectNode}
              onPropose={props.onPropose}
            />
            {connected.length === 0 ? null : (
              <div className="asset-browser-muted">{connected.length} connected graph node(s) are visible above.</div>
            )}
          </div>
        ) : null}

        {tab === "candidates" ? (
          <div className="asset-inspector-tab-stack">
            {candidates.length === 0 ? (
              <DetailBlock title="Candidate bindings">
                <div className="asset-browser-muted">No weak-evidence candidates in this payload.</div>
              </DetailBlock>
            ) : (
              <CandidateRelationGroup
                item={props.item}
                relations={candidates}
                selectedRelationId={props.selectedRelationId}
                actionResults={props.actionResults}
                onSelect={props.onSelectRelation}
                onSelectNode={props.onSelectNode}
                onPropose={props.onPropose}
                projectId={props.projectId}
              />
            )}
            <div className="asset-browser-muted">
              Candidate bindings are weak evidence. Queueing a candidate creates review work; graph truth changes only after Review Queue and commit/apply.
            </div>
          </div>
        ) : null}

        {tab === "actions" ? (
          <div className="asset-detail-grid asset-inspector-actions-grid">
            <DriftControls
              item={props.item}
              selectedRelation={props.selectedRelation}
              result={props.actionResults[`${props.item.asset_id}:drift`] ?? { state: "idle", message: "No observer state event recorded." }}
              proposalResult={
                props.actionResults[`${props.item.asset_id}:resolve-drift`] ?? { state: "idle", message: proposalStateLabel(props.item) }
              }
              onStateChange={props.onDriftStateChange}
              onResolve={props.onResolveDrift}
            />
            <HintBindingPanel
              item={props.item}
              nodeOptions={props.nodeOptions}
              draft={props.draft}
              result={props.attachResult}
              snapshotId={props.snapshotId}
              onUpdate={props.onUpdateDraft}
              onWrite={props.onWriteHint}
            />
          </div>
        ) : null}
      </div>
    </section>
  );
}

function RemoveBindingDialog(props: {
  state: RemoveConfirmState;
  projectId: string;
  onReasonChange(reason: string): void;
  onCancel(): void;
  onConfirm(): void;
}) {
  const sourceControlled = isSourceControlledUnbindCandidate(props.state.item, props.state.relation);
  const reason = props.state.reason.trim();
  return (
    <div className="modal-backdrop asset-confirm-backdrop" role="presentation">
      <div className="asset-confirm-dialog" role="dialog" aria-modal="true" aria-label="Confirm binding removal proposal">
        <div className="asset-detail-block-title">Confirm unbind operation</div>
        <p>
          This queues unbind for <span className="mono">{props.state.relation.target_node_id}</span>. It enters Review Queue,
          and graph truth changes only after the source file is committed and Update Graph runs.
        </p>
        <div className="asset-browser-muted">
          Operation path: {sourceControlled ? "source-controlled hint unbind" : "audit-only remove_binding fallback"}.
          {sourceControlled ? " If the backend endpoint rejects it, the fallback is visibly marked audit-only." : ""}
        </div>
        <label className="asset-confirm-reason">
          <span>Operator reason</span>
          <textarea
            value={props.state.reason}
            onChange={(event) => props.onReasonChange(event.target.value)}
            placeholder="Required: why should this binding be removed?"
          />
        </label>
        <div className="asset-browser-muted">
          Backend dependency: if source-controlled unbind is unavailable, the UI surfaces follow-up {REMOVE_BINDING_RUNTIME_DRIFT_ID}.
        </div>
        <div className="asset-confirm-actions">
          <button type="button" className="action-btn" onClick={props.onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="action-btn action-btn-primary"
            disabled={!reason}
            title={!reason ? "Operator reason is required." : "Queue unbind for Review Queue handling."}
            onClick={props.onConfirm}
          >
            Queue unbind
          </button>
        </div>
      </div>
    </div>
  );
}

function SelectedRelationOperation(props: {
  item: AssetInboxItem;
  relation: RelationView | null;
  result: ActionResult | null;
  projectId: string;
  onSelectNode?: (nodeId: string) => void;
  onPropose(item: AssetInboxItem, relation: RelationView, action: "attach_to_node" | "remove_binding"): void;
}) {
  if (!props.relation) {
    return <div className="asset-selected-relation-op asset-browser-muted">Select a relation branch to inspect its operation state.</div>;
  }
  const action = primaryRelationAction(props.relation);
  const result = props.result ?? { state: "idle", message: "No relation action recorded." };
  return (
    <div className="asset-selected-relation-op" aria-label="Selected relation operation result">
      <div className="asset-selected-relation-main">
        <span className="asset-map-root-label">Selected relation</span>
        <TargetNodeButton nodeId={props.relation.target_node_id} onSelectNode={props.onSelectNode} />
        <span className="asset-browser-muted">
          {RELATION_LABELS[props.relation.status] ?? props.relation.status} - {props.relation.target_title || "title n/a"}
        </span>
        {action ? (
          <span className="asset-browser-muted">
            Operation: {relationOperationLabel(action, isSourceControlledUnbindCandidate(props.item, props.relation))}
          </span>
        ) : null}
      </div>
      <div className="asset-selected-relation-actions">
        {action ? (
          <button
            type="button"
            className="action-btn action-btn-primary"
            disabled={result.state === "writing"}
            onClick={() => props.onPropose(props.item, props.relation as RelationView, action)}
          >
            {result.state === "writing" ? "Recording..." : relationActionLabel(action)}
          </button>
        ) : (
          <span className="asset-browser-muted">No direct add/remove operation is available for this relation.</span>
        )}
        <ActionResultLine result={result} projectId={props.projectId} />
      </div>
    </div>
  );
}

function TargetNodeButton(props: { nodeId?: string; onSelectNode?: (nodeId: string) => void }) {
  const nodeId = props.nodeId?.trim();
  if (!nodeId) return <span className="mono asset-unbound-target">unbound</span>;
  const content = (
    <>
      <span className="target-link-id">{nodeId}</span>
      <span className="target-link-arrow">→</span>
      <span className="target-link-hint">Graph</span>
    </>
  );
  if (!props.onSelectNode) {
    return (
      <span className="target-link target-link-static" aria-label={`Target node ${nodeId}`}>
        {content}
      </span>
    );
  }
  return (
    <button
      type="button"
      className="target-link asset-target-link"
      aria-label={`Open ${nodeId} in graph view`}
      onClick={(event) => {
        event.stopPropagation();
        props.onSelectNode?.(nodeId);
      }}
    >
      {content}
    </button>
  );
}

function ActionResultLine(props: { result?: ActionResult | null; projectId: string; compact?: boolean }) {
  if (!props.result?.message) return null;
  return (
    <span className={`attach-state attach-state-${props.result.state}${props.compact ? " attach-state-compact" : ""}`}>
      {props.result.message}
      {props.result.operationLabel ? (
        <>
          {" "}
          <span className="mono">[{props.result.operationLabel}]</span>
        </>
      ) : null}
      {props.result.sourceControlled ? (
        <>
          {" "}
          <span className="mono">source-controlled</span>
        </>
      ) : null}
      {props.result.auditOnly ? (
        <>
          {" "}
          <span className="mono">audit-only fallback</span>
        </>
      ) : null}
      {props.result.requiresCommit ? (
        <>
          {" "}
          <span className="mono">commit required</span>
        </>
      ) : null}
      {props.result.updateGraphAfterCommit ? (
        <>
          {" "}
          <span className="mono">Update Graph required</span>
        </>
      ) : null}
      {props.result.reviewQueueHref ? (
        <>
          {" "}
          <a className="asset-followup-link" href={props.result.reviewQueueHref}>
            Review Queue
          </a>
        </>
      ) : null}
      {props.result.followUpId ? (
        <>
          {" "}
          <a className="asset-followup-link" href={backlogFollowUpHref(props.projectId, props.result.followUpId)}>
            {props.result.followUpId}
          </a>
        </>
      ) : null}
    </span>
  );
}

function primaryRelationAction(relation: RelationView): "attach_to_node" | "remove_binding" | null {
  if (relation.status === "candidate" && relation.target_node_id) return "attach_to_node";
  if (["accepted", "impact_pending", "stale_drift"].includes(relation.status)) return "remove_binding";
  return null;
}

function relationActionResult(
  actionResults: Record<string, ActionResult>,
  relation: RelationView,
  actionOverride?: "attach_to_node" | "remove_binding" | null,
): ActionResult | null {
  const action = actionOverride ?? primaryRelationAction(relation);
  if (!action) return null;
  return actionResults[`${relation.relation_id}:${action}`] ?? null;
}

function relationActionLabel(action: "attach_to_node" | "remove_binding"): string {
  return action === "attach_to_node" ? "Queue add for review" : "Propose remove";
}

function relationOperationLabel(
  action: "attach_to_node" | "remove_binding" | null,
  sourceControlled = false,
): string {
  if (action === "attach_to_node") return "bind review";
  if (action === "remove_binding") return sourceControlled ? "source unbind" : "audit remove";
  return "n/a";
}

function isSourceControlledUnbindCandidate(item: AssetInboxItem, relation: RelationView): boolean {
  const role = relationRoleForAction(item, relation);
  return (
    ["accepted", "impact_pending", "stale_drift"].includes(relation.status) &&
    ["doc", "test", "config"].includes(role) &&
    canDirectWriteHint(item.path) &&
    Boolean(relation.target_node_id)
  );
}

function relationRoleForAction(item: AssetInboxItem, relation: RelationView): AttachRole {
  const role = normalizeAssetKind(relation.role);
  if (role === "test" || role === "config" || role === "doc") return role;
  return roleForAsset(item);
}

function sourceControlledUnbindMessage(message?: string): string {
  const suffix = message ? ` ${message}` : "";
  return `Source-controlled unbind written for Review Queue. Commit the changed file, then run Update Graph before trusting graph materialization.${suffix}`;
}

function auditActionMessage(action: "attach_to_node" | "remove_binding", result: FileHygieneActionResponse): string {
  const eventId = String(result.event?.event_id || result.action || action);
  const queued = result.review_queue?.queued ? " Review Queue item created." : " Review Queue status not reported.";
  if (action === "remove_binding") {
    return `Audit-only remove_binding event recorded: ${eventId}.${queued}`;
  }
  return `Proposal event recorded: ${eventId}.${queued} Accepted graph truth still depends on Review Queue handling.`;
}

function removeBindingRuntimeDriftMessage(error: ApiError): string {
  const detail = compactErrorDetail(error.body || error.message);
  return `Known remove_binding runtime drift. Backend returned ${error.status}; follow-up ${REMOVE_BINDING_RUNTIME_DRIFT_ID} tracks the server fix.${detail ? ` ${detail}` : ""}`;
}

function compactErrorDetail(value: string): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (!normalized) return "";
  return normalized.length > 220 ? `${normalized.slice(0, 217)}...` : normalized;
}

function backlogFollowUpHref(projectId: string, backlogId: string): string {
  const query = new URLSearchParams({
    project_id: projectId,
    view: "backlog",
    backlog: backlogId,
  });
  return `?${query.toString()}`;
}

function reviewQueueHref(projectId: string): string {
  return `?${new URLSearchParams({ project_id: projectId, view: "review" }).toString()}`;
}

function DriftControls(props: {
  item: AssetInboxItem;
  selectedRelation: RelationView | null;
  result: ActionResult;
  proposalResult: ActionResult;
  onStateChange(state: DriftStateName): void;
  onResolve(): void;
}) {
  const currentState = normalizeDriftState(props.item.drift?.state);
  return (
    <DetailBlock title="Observer drift review">
      <AssetActionFlow
        title="Drift/status flow"
        steps={["Commit changes", "Impact scope", "Possible drift", "Observer review", "Review Queue", "Resolved state"]}
      />
      <div className="asset-action-explain">
        <strong>{currentState === "not_drifted" ? "Baseline is not a human audit." : `${DRIFT_LABELS[currentState]} needs review context.`}</strong>
        <span>
          Users normally inspect the status here. Ask Observer to verify or resolve drift; accepted changes become trusted only after
          Review Queue and commit/apply.
        </span>
      </div>
      <div className="asset-drift-action-layout">
        <div className="asset-action-section">
          <div className="asset-action-subtitle">Current state</div>
          <div className="asset-state-readout">
            <span className={`asset-state-dot tone-${assetTone(props.item)}`} />
            <strong>{driftStateLabel(props.item)}</strong>
          </div>
          <div className="asset-action-help">
            Every commit can mark impacted files as possible drift. Files changed by the implementation can be verified current;
            unchanged impacted files should be reviewed by Observer.
          </div>
          <div className={`attach-state attach-state-${props.result.state}`}>{props.result.message}</div>
        </div>
        <div className="asset-action-section asset-action-section-emphasis">
          <div className="asset-action-subtitle">Ask Observer to resolve</div>
          <button
            type="button"
            className="action-btn action-btn-primary asset-resolve-drift-btn"
            disabled={props.proposalResult.state === "writing"}
            onClick={props.onResolve}
            title="Queue an AI-assisted drift proposal. Review Queue approval is required before it changes graph truth."
          >
            {props.proposalResult.state === "writing" ? "Queueing..." : "Queue Observer review"}
          </button>
          <div className="asset-action-help">
            Creates a review-gated proposal for the selected relation. It becomes effective only after review and commit/apply.
          </div>
          <div className={`attach-state attach-state-${props.proposalResult.state}`}>{props.proposalResult.message}</div>
        </div>
      </div>
      {props.selectedRelation ? (
        <div className="asset-browser-muted">
          Target relation: {props.selectedRelation.target_node_id || RELATION_LABELS[props.selectedRelation.status] || "unbound"}
        </div>
      ) : null}
    </DetailBlock>
  );
}

function RelationPanel(props: {
  item: AssetInboxItem;
  relations: RelationView[];
  selectedRelationId: string;
  actionResults: Record<string, ActionResult>;
  onSelect(relationId: string): void;
  onSelectNode?: (nodeId: string) => void;
  onPropose(item: AssetInboxItem, relation: RelationView, action: "attach_to_node" | "remove_binding"): void;
  projectId: string;
}) {
  const accepted = props.relations.filter((relation) =>
    ["accepted", "impact_pending", "stale_drift"].includes(relation.status),
  );
  const unbound = props.relations.filter((relation) => relation.status === "unbound");
  if (props.relations.length === 0) {
    return (
      <div className="asset-browser-empty">
        No trusted graph relations for <span className="mono">{props.item.path}</span>.
      </div>
    );
  }
  return (
    <div className="asset-relation-stack scrollbar-thin">
      <RelationGroup
        title="Accepted / drift"
        item={props.item}
        relations={accepted}
        empty="No accepted binding."
        selectedRelationId={props.selectedRelationId}
        actionResults={props.actionResults}
        onSelect={props.onSelect}
        onSelectNode={props.onSelectNode}
        onPropose={props.onPropose}
        projectId={props.projectId}
      />
      <RelationGroup
        title="Unbound"
        item={props.item}
        relations={unbound}
        empty="No unbound relation."
        selectedRelationId={props.selectedRelationId}
        actionResults={props.actionResults}
        onSelect={props.onSelect}
        onSelectNode={props.onSelectNode}
        onPropose={props.onPropose}
        projectId={props.projectId}
      />
    </div>
  );
}

function RelationGroup(props: {
  title: string;
  item: AssetInboxItem;
  relations: RelationView[];
  empty: string;
  selectedRelationId: string;
  actionResults: Record<string, ActionResult>;
  onSelect(relationId: string): void;
  onSelectNode?: (nodeId: string) => void;
  onPropose(item: AssetInboxItem, relation: RelationView, action: "attach_to_node" | "remove_binding"): void;
  projectId: string;
}) {
  return (
    <section className="asset-relation-group">
      <div className="asset-relation-group-title">
        <span>{props.title}</span>
        <span className="asset-chip-count">{props.relations.length}</span>
      </div>
      {props.relations.length === 0 ? (
        <div className="asset-browser-muted">{props.empty}</div>
      ) : (
        <ul className="link-list asset-relation-list">
          {props.relations.map((relation) => {
            const active = props.selectedRelationId === relation.relation_id;
            const action = primaryRelationAction(relation);
            const actionKey = action ? `${relation.relation_id}:${action}` : "";
            const result = action ? props.actionResults[actionKey] : null;
            return (
              <li key={relation.relation_id}>
                <div
                  className={`link-row asset-relation-row ${relationStatusClass(relation.status)}${active ? " active" : ""}`}
                  role="button"
                  tabIndex={0}
                  aria-pressed={active}
                  onClick={() => props.onSelect(relation.relation_id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      props.onSelect(relation.relation_id);
                    }
                  }}
                >
                  <span className={`asset-relation-state ${relationStatusClass(relation.status)}`}>
                    {RELATION_LABELS[relation.status] ?? relation.status}
                  </span>
                  <TargetNodeButton nodeId={relation.target_node_id} onSelectNode={props.onSelectNode} />
                  <span className="link-name" title={relation.target_title || relation.target_node_id || "unbound"}>
                    {relation.target_title || relation.target_node_id || "Unbound asset"}
                  </span>
                  <span className="asset-relation-row-meta">
                    <span>{relation.role || "role n/a"}</span>
                    <span>{relation.evidence_kind || relation.source || "evidence n/a"}</span>
                    <span>{formatImpactScope(relation.impact_scope)}</span>
                    {relation.proposal_hash ? <span className="mono">{relation.proposal_hash}</span> : null}
                  </span>
                  <span className="asset-relation-row-actions">
                    {action ? (
                      <button
                        type="button"
                        className="action-btn"
                        disabled={result?.state === "writing"}
                        onClick={(event) => {
                          event.stopPropagation();
                          props.onSelect(relation.relation_id);
                          props.onPropose(props.item, relation, action);
                        }}
                      >
                        {relationActionLabel(action)}
                      </button>
                    ) : null}
                    <ActionResultLine result={result} projectId={props.projectId} compact />
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function CandidateRelationGroup(props: {
  item: AssetInboxItem;
  relations: RelationView[];
  selectedRelationId: string;
  actionResults: Record<string, ActionResult>;
  onSelect(relationId: string): void;
  onSelectNode?: (nodeId: string) => void;
  onPropose(item: AssetInboxItem, relation: RelationView, action: "attach_to_node" | "remove_binding"): void;
  projectId: string;
}) {
  return (
    <section className="asset-relation-group">
      <div className="asset-relation-group-title">
        <span>Candidate bindings</span>
        <span className="asset-chip-count">{props.relations.length}</span>
      </div>
      <ul className="link-list asset-relation-list asset-candidate-list">
        {props.relations.map((relation) => {
          const active = props.selectedRelationId === relation.relation_id;
          const result = props.actionResults[`${relation.relation_id}:attach_to_node`] ?? null;
          const precheck: Partial<AssetInboxBindingCandidate["precheck"]> = relation.precheck ?? {};
          return (
            <li key={relation.relation_id}>
              <div
                className={`link-row asset-relation-row asset-candidate-row ${relationStatusClass(relation.status)}${active ? " active" : ""}`}
                role="button"
                tabIndex={0}
                aria-pressed={active}
                onClick={() => props.onSelect(relation.relation_id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    props.onSelect(relation.relation_id);
                  }
                }}
              >
                <span className={`asset-relation-state ${relationStatusClass(relation.status)}`}>
                  {RELATION_LABELS[relation.status] ?? relation.status}
                </span>
                <TargetNodeButton nodeId={relation.target_node_id} onSelectNode={props.onSelectNode} />
                <span className="link-name" title={relation.target_title || relation.target_node_id || "candidate"}>
                  {relation.target_title || relation.target_node_id || "Candidate target"}
                </span>
                <span className="asset-relation-row-meta asset-candidate-reason">
                  <span title={relation.source || "source n/a"}>Source {relation.source || "n/a"}</span>
                  <span title={relation.evidence_kind || "evidence n/a"}>Evidence {relation.evidence_kind || "n/a"}</span>
                  <span>Strength {relation.binding_strength || precheck.binding_strength || "weak"}</span>
                  <span>Decision {precheck.decision || (relation.review_required ? "review_required" : "n/a")}</span>
                  <span>{precheck.ok === false ? "Precheck failed" : "Precheck ok"}</span>
                  {relation.proposal_hash ? (
                    <span className="mono" title={relation.proposal_hash}>
                      {relation.proposal_hash}
                    </span>
                  ) : null}
                </span>
                <span className="asset-relation-row-actions">
                  <button
                    type="button"
                    className="action-btn"
                    disabled={result?.state === "writing"}
                    onClick={(event) => {
                      event.stopPropagation();
                      props.onSelect(relation.relation_id);
                      props.onPropose(props.item, relation, "attach_to_node");
                    }}
                  >
                    Queue add for review
                  </button>
                  <ActionResultLine result={result} projectId={props.projectId} compact />
                </span>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function HintBindingPanel(props: {
  item: AssetInboxItem;
  nodeOptions: NodeRecord[];
  draft: AttachDraft;
  result: AttachResult;
  snapshotId: string;
  onUpdate(patch: Partial<AttachDraft>): void;
  onWrite(): void;
}) {
  const supported = canDirectWriteHint(props.item.path);
  const hintable = isHintable(props.item);
  const selectedNode = props.nodeOptions.find((node) => node.node_id === props.draft.targetNodeId) ?? null;
  const disabledReason = governanceHintDisabledReason(props.item, supported, props.nodeOptions.length, props.snapshotId);
  const canUseSourceHint = supported && hintable && props.snapshotId && !disabledReason;
  return (
    <DetailBlock title="Observer binding review">
      <AssetActionFlow
        title="Binding flow"
        steps={["Unbound file", "Candidate evidence", "Observer proposal", "Review Queue", "Commit", "Update Graph"]}
      />
      <div className="asset-action-explain">
        <strong>Binding changes are review-gated.</strong>
        <span>
          Users inspect candidates and status here. Observer should decide whether to queue a candidate, write a source hint, or
          leave the asset unbound.
        </span>
      </div>
      <div className="asset-action-section">
        <div className="asset-action-subtitle">Observer action path</div>
        <div className="asset-action-help">
          {canUseSourceHint
            ? "Source hint path is available for Observer: write source evidence, commit it, then update the graph."
            : disabledReason || "Use candidate review actions for this asset."}
        </div>
        {selectedNode ? (
          <div className="asset-node-selected-row">
            <span>Suggested target</span>
            <strong>{nodeOptionLabel(selectedNode)}</strong>
            <span className="mono">{selectedNode.node_id}</span>
            <span className="asset-browser-muted">{nodeFileHint(selectedNode)}</span>
          </div>
        ) : null}
      </div>
      <div className={`attach-state attach-state-${props.result.state}`}>
        {props.result.state === "idle" ? "No source hint written from this inspector." : props.result.message}
      </div>
    </DetailBlock>
  );
}

function AssetActionFlow({ title, steps }: { title: string; steps: string[] }) {
  return (
    <div className="asset-action-flow-block">
      <div className="asset-action-subtitle">{title}</div>
      <div className="asset-guide-flow">
        {steps.map((step) => (
          <span key={step}>{step}</span>
        ))}
      </div>
    </div>
  );
}

function DetailBlock({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="asset-detail-block">
      <div className="asset-detail-block-title">{title}</div>
      {children}
    </section>
  );
}

function Meta({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="asset-meta-cell">
      <span>{label}</span>
      <strong className={mono ? "mono" : undefined}>{value}</strong>
    </div>
  );
}

function buildGroups(assetInbox: AssetInboxResponse, items: AssetInboxItem[]): Record<AssetGroupId, GroupView> {
  const groups = Object.fromEntries(GROUP_ORDER.map((id) => [id, emptyGroup(id)])) as Record<AssetGroupId, GroupView>;
  const backendGroups = assetInbox.asset_groups ?? [];
  if (backendGroups.length > 0) {
    for (const backendGroup of backendGroups) {
      const id = normalizeGroupId(backendGroup.group_id ?? backendGroup.group);
      const target = groups[id];
      const itemIds = new Set([
        ...(backendGroup.item_ids ?? []),
        ...(backendGroup.items ?? []).map((item) => item.asset_id),
      ].filter(Boolean));
      const paths = new Set([
        ...(backendGroup.paths ?? []),
        ...(backendGroup.items ?? []).map((item) => item.path),
      ].filter(Boolean));
      for (const item of items) {
        if (itemIds.has(item.asset_id) || paths.has(item.path)) target.itemIds.add(item.asset_id);
      }
      target.count = Math.max(target.count, backendGroup.count ?? target.itemIds.size);
      target.statuses = mergeStatusCounts(target.statuses, backendGroup.status_counts ?? backendGroup.statuses ?? {});
    }
  }

  for (const item of items) {
    const id = normalizeGroupId(item.asset_kind, item.asset_status, item.path);
    groups.ALL.itemIds.add(item.asset_id);
    groups.ALL.statuses[item.asset_status] = (groups.ALL.statuses[item.asset_status] ?? 0) + 1;
    if (backendGroups.length === 0 || !groups[id].itemIds.has(item.asset_id)) {
      groups[id].itemIds.add(item.asset_id);
      groups[id].statuses[item.asset_status] = (groups[id].statuses[item.asset_status] ?? 0) + 1;
    }
  }

  for (const id of GROUP_ORDER) {
    if (id === "ALL") {
      groups[id].count = items.length;
    } else if (groups[id].count === 0 || groups[id].itemIds.size > groups[id].count) {
      groups[id].count = groups[id].itemIds.size;
    }
  }
  return groups;
}

function emptyGroup(id: AssetGroupId): GroupView {
  return { id, label: GROUP_LABELS[id], count: 0, itemIds: new Set(), statuses: {} };
}

function mergeStatusCounts(left: Record<string, number>, right: Record<string, number>): Record<string, number> {
  const merged = { ...left };
  for (const [status, count] of Object.entries(right)) {
    merged[status] = (merged[status] ?? 0) + count;
  }
  return merged;
}

function filterAssets(
  items: AssetInboxItem[],
  groups: Record<AssetGroupId, GroupView>,
  groupFilter: AssetGroupId,
  bucketFilter: string,
  statusFilter: AssetStatusFilter,
  query: string,
): AssetInboxItem[] {
  const q = query.trim().toLowerCase();
  const group = groups[groupFilter];
  return items.filter((item) => {
    if (groupFilter !== "ALL" && group && !group.itemIds.has(item.asset_id)) return false;
    if (bucketFilter && !assetBucketMatches(item, bucketFilter)) return false;
    if (statusFilter !== "all" && !assetStatusFilterMatches(item, statusFilter)) return false;
    if (!q) return true;
    const relations = deriveRelations(item);
    const hay = [
      item.path,
      item.asset_kind,
      item.asset_status,
      item.graph_status,
      item.scan_status,
      ...(item.evidence ?? []).map((evidence) => `${evidence.kind} ${evidence.message}`),
      ...relations.map((relation) =>
        [
          relation.status,
          relation.role,
          relation.target_node_id,
          relation.target_title,
          relation.source,
          relation.evidence_kind,
          relation.proposal_hash,
        ].join(" "),
      ),
    ]
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  });
}

function assetBucketMatches(item: AssetInboxItem, bucketId: string): boolean {
  if (!bucketId || bucketId === "all") return true;
  if (bucketId === "health" || bucketId === "accepted") return assetStatusFilterMatches(item, "health");
  if (bucketId === "candidate") return assetStatusFilterMatches(item, "candidate");
  if (bucketId === "drift") return assetStatusFilterMatches(item, "drift");
  if (bucketId === "orphan" || bucketId === "unbound") return assetStatusFilterMatches(item, "orphan");
  if (bucketId === "ignored") return item.asset_status === "ignored" || item.asset_status === "archive";
  if (bucketId === "pending") return item.asset_status.includes("pending") || item.asset_status.includes("decision");
  return item.asset_status === bucketId;
}

function assetStatusFilterMatches(item: AssetInboxItem, filter: AssetStatusFilter): boolean {
  if (filter === "health") {
    return item.asset_status === "accepted" || item.binding_status === "accepted" || (item.accepted_bindings ?? []).length > 0 || item.graph_status === "current";
  }
  if (filter === "candidate") {
    return item.asset_status.includes("candidate") || item.asset_status.includes("pending") || (item.binding_candidates ?? []).length > 0;
  }
  if (filter === "drift") {
    return item.asset_status.includes("drift") || item.asset_status === "stale" || normalizeDriftState(item.drift?.state) !== "not_drifted";
  }
  if (filter === "orphan") {
    return item.asset_status.includes("orphan") || item.asset_status.includes("unbound") || item.scan_status === "orphan" || (item.accepted_bindings ?? []).length === 0;
  }
  return true;
}

function deriveRelations(item: AssetInboxItem): RelationView[] {
  if ((item.mount_relations ?? []).length > 0) {
    return (item.mount_relations ?? []).map((relation, index) => ({
      ...relation,
      relation_id: relation.relation_id || `${item.asset_id}:relation:${index}`,
      status: relation.status || "candidate",
      target_node_id: relation.target_node_id,
    }));
  }
  const accepted = (item.accepted_bindings ?? []).map((binding): RelationView => ({
    relation_id: `${item.asset_id}:accepted:${binding.node_id}:${binding.role}`,
    status: "accepted",
    role: binding.role,
    target_node_id: binding.node_id,
    target_title: binding.title,
    source: binding.source,
    evidence_kind: "accepted_binding",
    binding_strength: "strong",
    impact_scope: "accepted_binding",
    review_required: false,
  }));
  const candidates = (item.binding_candidates ?? []).map((candidate, index): RelationView => ({
    relation_id: `${item.asset_id}:candidate:${candidate.proposal_hash || index}`,
    status: "candidate",
    role: normalizeAssetKind(candidate.asset_kind),
    target_node_id: candidate.target_node_id,
    target_title: candidate.target_title,
    source: candidate.source,
    evidence_kind: candidate.evidence_kind,
    proposal_hash: candidate.proposal_hash,
    binding_strength: candidate.precheck?.binding_strength,
    impact_scope: candidate.precheck?.mode || "proposal",
    review_required: candidate.precheck?.decision !== "accepted",
    precheck: candidate.precheck,
  }));
  return [...accepted, ...candidates];
}

function summarizeRelations(item: AssetInboxItem, relations: RelationView[]) {
  const accepted = relations.filter((relation) => relation.status === "accepted").length;
  const candidate = relations.length - accepted;
  return {
    accepted_count: item.relation_summary?.accepted_count ?? accepted,
    candidate_count: item.relation_summary?.candidate_count ?? candidate,
    relation_count: item.relation_summary?.relation_count ?? relations.length,
    impact_scope_count:
      item.relation_summary?.impact_scope_count ??
      relations.filter((relation) => relation.status === "accepted" || relation.impact_scope).length,
    review_required_count:
      item.relation_summary?.review_required_count ?? relations.filter((relation) => relation.review_required).length,
  };
}

function relationSummaryLabel(summary: ReturnType<typeof summarizeRelations> | null): string {
  if (!summary) return "0 relations";
  return `${summary.accepted_count ?? 0} accepted / ${summary.candidate_count ?? 0} candidate`;
}

function driftStateLabel(item: AssetInboxItem): string {
  const state = normalizeDriftState(item.drift?.state);
  const source = (item.drift?.source || "").trim();
  if (state === "not_drifted") {
    if (!source || source === "default") return "Baseline";
    if (/(observer|gate|commit|impact|contract|verified|review)/i.test(source)) return "Verified current";
    return `Current (${source})`;
  }
  return `${DRIFT_LABELS[state]}${source ? ` (${source})` : ""}`;
}

function proposalStateLabel(item: AssetInboxItem): string {
  if (item.drift_proposal?.proposal_id) {
    return `Latest proposal ${item.drift_proposal.status || "recorded"} / ${item.drift_proposal.ai_status || "ai n/a"}`;
  }
  if (item.drift?.impact_pending) return "Impact reminder present; Resolve Drift will queue or record a proposal.";
  return "No drift proposal queued.";
}

function normalizeDriftState(state?: string): DriftStateName {
  if (state === "suspected" || state === "confirmed" || state === "resolved" || state === "waived") return state;
  return "not_drifted";
}

function relationStatusClass(status?: string): string {
  if (status === "accepted") return "accepted";
  if (status === "candidate") return "candidate";
  if (status === "impact_pending") return "impact-pending";
  if (status === "stale_drift") return "stale-drift";
  if (status === "unbound") return "unbound";
  return "unbound";
}

function assetInspectorTone(tone: "green" | "amber" | "red" | "gray"): "green" | "amber" | "red" | "neutral" {
  return tone === "gray" ? "neutral" : tone;
}

function normalizeGroupId(kind?: string, status?: string, path?: string): AssetGroupId {
  const normalized = normalizeAssetKind(kind);
  if (normalized === "doc") return "doc";
  if (normalized === "test") return "test";
  if (normalized === "config") return "config";
  if (normalized === "source") return "source";
  if (normalized === "generated" || normalized === "ignored" || status === "ignored" || status === "archive") {
    return "generated";
  }
  const lowerPath = (path || "").toLowerCase();
  if (lowerPath.includes("/test") || lowerPath.endsWith(".test.ts") || lowerPath.endsWith(".spec.ts")) return "test";
  if (lowerPath.endsWith(".md") || lowerPath.endsWith(".mdx") || lowerPath.includes("/docs/")) return "doc";
  if (/\.(ya?ml|toml|ini|cfg|json)$/.test(lowerPath)) return "config";
  return "other";
}

function normalizeAssetKind(kind?: string): string {
  const value = (kind || "").trim().toLowerCase();
  if (value === "index_doc") return "doc";
  if (value === "unknown") return "other";
  return value;
}

function labelForKind(kind?: string): string {
  const normalized = normalizeAssetKind(kind);
  if (normalized === "doc") return "Doc";
  if (normalized === "test") return "Test";
  if (normalized === "config") return "Config";
  if (normalized === "source") return "Source";
  if (normalized === "generated") return "Generated";
  if (normalized === "ignored") return "Ignored";
  return "Other";
}

function roleForAsset(item?: AssetInboxItem): AttachRole {
  const kind = normalizeAssetKind(item?.asset_kind);
  if (kind === "test") return "test";
  if (kind === "config") return "config";
  return "doc";
}

function isHintable(item: AssetInboxItem): boolean {
  const kind = normalizeAssetKind(item.asset_kind);
  return (
    ["doc", "test", "config"].includes(kind) &&
    (item.accepted_bindings ?? []).length === 0 &&
    item.scan_status === "orphan" &&
    ["doc_unbound"].includes(item.asset_status)
  );
}

function nodeOptionLabel(node: NodeRecord): string {
  return `${node.title || node.node_id} - ${node.node_id}`;
}

function nodeFileHint(node: NodeRecord): string {
  const files = [
    ...(node.primary_files ?? []),
    ...(node.secondary_files ?? []),
    ...(node.test_files ?? []),
    ...(node.config_files ?? []),
  ];
  if (files.length === 0) return "no files listed";
  const first = files[0];
  return files.length === 1 ? first : `${first} +${files.length - 1}`;
}

function governanceHintDisabledReason(
  item: AssetInboxItem,
  supported: boolean,
  nodeCount: number,
  snapshotId: string,
): string {
  if (!snapshotId) return "Select a snapshot before writing governance hints.";
  if (!supported) return "Direct write unsupported for this file type.";
  const kind = normalizeAssetKind(item.asset_kind);
  if (!["doc", "test", "config"].includes(kind)) return "Governance hint is only available for doc/test/config assets.";
  if ((item.accepted_bindings ?? []).length > 0) return "Governance hint is disabled because this asset is already attached.";
  if (item.scan_status !== "orphan") {
    return `Governance hint is disabled: backend requires scan_status=orphan, current scan_status=${item.scan_status || "unknown"}.`;
  }
  if (item.asset_status !== "doc_unbound") {
    return `Governance hint is disabled for ${STATUS_LABELS[item.asset_status] ?? item.asset_status}; use proposal/review actions instead.`;
  }
  if (nodeCount === 0) return "No target nodes are available in this snapshot.";
  return "";
}

function suggestedTargetNodeId(item: AssetInboxItem | undefined, nodes: NodeRecord[]): string {
  const candidateTarget = item?.binding_candidates?.find((candidate) => candidate.target_node_id)?.target_node_id;
  if (candidateTarget && nodes.some((node) => node.node_id === candidateTarget)) return candidateTarget;
  return nodes[0]?.node_id ?? "";
}

function canDirectWriteHint(path: string): boolean {
  const lower = path.toLowerCase();
  const name = lower.split(/[\\/]/).pop() || "";
  if (name === "dockerfile" || name === "makefile") return true;
  return [
    ".md",
    ".mdx",
    ".html",
    ".htm",
    ".py",
    ".pyw",
    ".sh",
    ".bash",
    ".ps1",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".txt",
    ".rst",
    ".adoc",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
  ].some((suffix) => lower.endsWith(suffix));
}

function compareAssets(a: AssetInboxItem, b: AssetInboxItem): number {
  const byGroup = GROUP_ORDER.indexOf(normalizeGroupId(a.asset_kind, a.asset_status, a.path)) -
    GROUP_ORDER.indexOf(normalizeGroupId(b.asset_kind, b.asset_status, b.path));
  if (byGroup !== 0) return byGroup;
  const byState = statusWeight(a.asset_status) - statusWeight(b.asset_status);
  if (byState !== 0) return byState;
  return a.path.localeCompare(b.path);
}

function statusWeight(status: string): number {
  const order = [
    "source_orphan",
    "doc_unbound",
    "doc_candidate",
    "test_candidate",
    "config_pending_decision",
    "impact_pending",
    "drift_suspected",
    "drift_confirmed",
    "stale",
    "accepted",
    "drift_resolved",
    "drift_waived",
    "ignored",
    "archive",
  ];
  const index = order.indexOf(status as AssetInboxStatus);
  return index < 0 ? 99 : index;
}

function assetStatusClass(status: string): string {
  if (status === "accepted") return "qa";
  if (status === "impact_pending" || status === "drift_confirmed") return "failed";
  if (status === "drift_suspected") return "running";
  if (status === "drift_resolved" || status === "drift_waived") return "qa";
  if (status === "ignored" || status === "archive") return "muted";
  if (status === "source_orphan" || status === "stale") return "failed";
  if (status === "doc_candidate" || status === "test_candidate" || status === "config_pending_decision") return "running";
  return "queued";
}

function assetTone(item: AssetInboxItem): "green" | "amber" | "red" | "gray" {
  const status = item.asset_status;
  if (status === "accepted" || status === "drift_resolved" || status === "drift_waived") return "green";
  if (status.includes("candidate") || status.includes("pending") || status === "drift_suspected") return "amber";
  if (status.includes("orphan") || status.includes("unbound") || status === "drift_confirmed" || status === "stale") return "red";
  if (status === "ignored" || status === "archive" || normalizeAssetKind(item.asset_kind) === "generated") return "gray";
  return "gray";
}

function countStatus(assetInbox: AssetInboxResponse, status: string): number {
  return assetInbox.summary?.by_status?.[status] ?? 0;
}

function formatBytes(size?: number): string {
  if (size == null || Number.isNaN(size)) return "n/a";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatImpactScope(scope?: boolean | string | string[]): string {
  if (Array.isArray(scope)) return scope.length === 0 ? "n/a" : scope.join(", ");
  if (scope === true) return "yes";
  if (scope === false) return "no";
  return scope || "n/a";
}
