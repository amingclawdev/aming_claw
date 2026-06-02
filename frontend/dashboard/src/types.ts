// Type definitions mirror live shapes returned by the governance HTTP API
// (http://localhost:40000). They are intentionally narrow to what the P0
// dashboard needs — additional fields are tolerated.

export interface HealthResponse {
  status: string;
  service: string;
  port: number;
  version: string;
  pid: number;
  request_id: string;
}

export interface StatusResponse {
  ok: boolean;
  project_id: string;
  active_snapshot_id: string;
  graph_snapshot_commit: string;
  materialized_graph_baseline_commit: string;
  scan_baseline_commit: string;
  scan_baseline_id: number;
  pending_scope_reconcile_count: number;
  pending_scope_reconcile: unknown[];
  current_state?: {
    snapshot_id?: string;
    graph_stale?: {
      is_stale: boolean;
      active_graph_commit: string;
      head_commit: string;
      changed_files?: string[];
      changed_file_count?: number;
    };
  };
}

export interface RawRequirement {
  kind?: "raw_requirement";
  raw_id: string;
  project_id: string;
  raw_text: string;
  source?: string;
  session_id?: string;
  captured_by?: string;
  status: "raw_inbox" | "needs_confirmation" | "promoted" | "dismissed" | string;
  note?: string;
  promoted_bug_id?: string;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export type ObserverCommandStatus =
  | "queued"
  | "notified"
  | "claimed"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | string;

export interface ObserverSessionRecord {
  session_id: string;
  project_id: string;
  observer_kind?: string;
  session_label?: string;
  pid?: number;
  cwd?: string;
  capabilities?: Record<string, unknown>;
  status: string;
  computed_status?: "active" | "idle" | "stale" | "closed" | "revoked" | string;
  registered_at: string;
  last_seen_at: string;
  closed_at?: string;
  revoked_at?: string;
}

export interface ObserverConnectionSummary {
  connected: boolean;
  connected_count: number;
  active_count: number;
  stale_count: number;
  sessions: ObserverSessionRecord[];
  heartbeat_interval_sec: number;
}

export interface ObserverCommand {
  command_id: string;
  project_id: string;
  command_type: string;
  payload?: Record<string, unknown>;
  status: ObserverCommandStatus;
  target_session_id?: string;
  claimed_by_session_id?: string;
  created_by?: string;
  created_at: string;
  notified_at?: string;
  claimed_at?: string;
  completed_at?: string;
  result?: Record<string, unknown>;
  error?: string;
}

export interface ObserverCommandSummary {
  count: number;
  counts: Record<string, number>;
  items: ObserverCommand[];
}

export type ProjectInboxItem = RawRequirement | (BacklogBug & { kind: "backlog" });

export interface ProjectInboxLane {
  count: number;
  items: ProjectInboxItem[];
  source?: string;
}

export interface ProjectInboxResponse {
  ok: boolean;
  project_id: string;
  homepage_view: "project_inbox" | string;
  observer?: ObserverConnectionSummary;
  observer_commands?: ObserverCommandSummary;
  lanes: {
    raw_inbox: ProjectInboxLane;
    needs_confirmation: ProjectInboxLane;
    ready_backlog: ProjectInboxLane;
    in_progress: ProjectInboxLane;
    review_needed: ProjectInboxLane;
    done: ProjectInboxLane;
  };
  request_id?: string;
}

export interface ActiveSummaryResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  commit_sha: string;
  snapshot_kind: string;
  snapshot_status: string;
  created_at: string;
  graph_sha256: string;
  inventory_sha256: string;
  drift_sha256: string;
  counts: SummaryCounts;
  health: SummaryHealth;
}

export interface SummaryCounts {
  nodes: number;
  nodes_by_layer: Record<string, number>;
  edges: number;
  edges_by_type: Record<string, number>;
  features: number;
  files: number;
  orphan_files: number;
  pending_decision_files: number;
  cleanup_candidates: number;
  ai_review_feedback: number;
}

export interface SummaryHealth {
  project_health_score: number;
  raw_project_health_score: number;
  file_hygiene_score: number;
  artifact_binding_score: number;
  governance_observability_score: number;
  doc_coverage_ratio: number;
  test_coverage_ratio: number;
  semantic_coverage_ratio: number;
  structure_health_score: number;
  semantic_health_score: number;
  project_insight_health_score: number;
  semantic_health: SemanticHealthBlock;
  structure_health?: { feature_count?: number; governed_feature_count?: number };
}

export interface SemanticHealthBlock {
  score: number;
  feature_count: number;
  semantic_current_count: number;
  semantic_missing_count: number;
  semantic_stale_count: number;
  semantic_unverified_hash_count: number;
  semantic_current_ratio: number;
  edge_semantic_eligible_count: number;
  edge_semantic_current_count: number;
  edge_semantic_requested_count: number;
  edge_semantic_missing_count: number;
}

export type Layer = "L1" | "L2" | "L3" | "L4" | "L7";

export interface NodeRecord {
  node_id: string;
  layer: Layer | string;
  title: string;
  kind?: string;
  primary_files?: string[];
  secondary_files?: string[];
  test_files?: string[];
  config_files?: string[];
  metadata?: NodeMetadata;
  semantic?: NodeSemantic;
  exclude_as_feature?: boolean;
  // MF-016/017 follow-up: per-node feature-health score (prototype algorithm).
  // null for L4 asset leaves and empty containers. Populated by lib/health.ts
  // after the dashboard data bundle loads. L4 nodes are intentionally
  // unscored — they're config/asset files, no health concept applies.
  _health?: number | null;
}

export interface NodeMetadata {
  hierarchy_parent?: string;
  children?: string[];
  module?: string;
  file_role?: string;
  feature_hash?: string;
  function_count?: number;
  functions?: string[];
  // Per-function line metadata persisted by the graph adapter (since 59c9fbc).
  // Map key is the short symbol name (`DecisionValidator.__init__`), value is
  // a `[start_line, end_line]` 1-based pair.
  function_lines?: Record<string, [number, number]>;
  function_calls?: FunctionCallFact[];
  function_called_by?: FunctionCallFact[];
  function_weak_calls?: FunctionWeakCallFact[];
  function_call_count?: number;
  function_called_by_count?: number;
  function_weak_call_count?: number;
  graph_metrics?: {
    fan_in?: number;
    fan_out?: number;
    hierarchy_in?: number;
    hierarchy_out?: number;
  };
  exclude_as_feature?: boolean;
  feature_metadata?: { exclude_as_feature?: boolean };
  quality_flags?: string[];
  // L4 asset metadata. asset_key encodes "<kind>:<path|name>" — for
  // file-backed kinds (config, artifact) it's the surrogate path the
  // backend uses to look up the asset. The dashboard reads it as a
  // fallback when primary_files is empty (which is the case for every
  // L4 node today — backend doesn't populate primary_files for L4).
  asset_key?: string;
  kind?: string;
  aggregate_asset?: boolean;
}

export interface FunctionCallFact {
  caller?: string;
  caller_short?: string;
  caller_module?: string;
  caller_file?: string;
  caller_line?: [number, number];
  callee?: string;
  callee_short?: string;
  callee_module?: string;
  callee_file?: string;
  callee_line?: [number, number];
  confidence?: string;
  resolution?: string;
}

export interface FunctionWeakCallFact {
  caller?: string;
  caller_short?: string;
  caller_module?: string;
  caller_file?: string;
  caller_line?: [number, number];
  raw_target?: string;
  candidates?: string[];
  confidence?: string;
  resolution?: string;
  reason?: string;
}

export interface NodeSemantic {
  status?: string;
  node_status?: string;
  job_status?: string;
  feature_hash?: string;
  hash_state?: string;
  has_semantic_payload?: boolean;
  feature_name?: string;
  domain_label?: string;
  intent?: string;
  semantic_summary?: string;
  doc_status?: string;
  test_status?: string;
  config_status?: string;
  feedback_round?: number;
  open_issues?: unknown[];
  observer_decision?: string;
  review_status?: string;
  validity?: NodeValidity;
  carried_forward_from_snapshot_id?: string;
  carried_forward_at?: string;
  ai_route?: { provider?: string; model?: string };
}

export interface NodeValidity {
  status?: string;
  hash_validation?: string;
  file_hash_status?: string;
  valid?: boolean;
  feature_hash_match?: boolean;
  file_hash_match?: boolean;
  hash_state?: string;
  hash_verified?: boolean;
  current_feature_hash?: string;
  stored_feature_hash?: string;
  semantic_status?: string;
}

export interface NodesResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  nodes: NodeRecord[];
  count: number;
}

export interface EdgeRecord {
  src: string;
  dst: string;
  type?: string;
  edge_type?: string;
  evidence?: string;
  direction?: string;
  confidence?: number;
}

export interface EdgesResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  edges: EdgeRecord[];
  count: number;
}

export interface ProjectionResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  projection_id?: string;
  event_watermark?: string;
  base_commit?: string;
  // null when the snapshot's semantic projection has not been computed yet
  // (e.g. immediately after a /reconcile/full activation).
  projection: {
    project_id: string;
    snapshot_id: string;
    commit_sha: string;
    schema_version: number;
    node_semantics: Record<string, ProjectionNodeEntry>;
    edge_semantics: Record<string, unknown>;
    health_review: unknown;
  } | null;
  health: Record<string, unknown>;
}

export interface ProjectionNodeEntry {
  node_id: string;
  semantic: NodeSemantic;
  validity: NodeValidity;
  source_event?: { event_id?: string; event_seq?: number; updated_at?: string };
  stable_node_key?: string;
}

export interface OperationsQueueResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  active_snapshot_id: string;
  count: number;
  operations: OperationRow[];
  summary: OperationsSummary;
}

export interface OperationRow {
  operation_id: string;
  operation_type: string;
  target_scope: string;
  target_id: string;
  target_label: string;
  status: string;
  progress: { done: number; total: number };
  created_at: string;
  updated_at: string;
  claimed_by: string;
  worker_id: string;
  lease_expires_at: string;
  last_error: string;
  last_result: string;
  trace_id: string;
  supported_actions: string[];
}

export interface OperationsSummary {
  by_type: Record<string, number>;
  by_status: Record<string, number>;
  pending_scope_reconcile_count: number;
  semantic_denominators?: {
    node_current: number;
    node_unverified: number;
    node_missing: number;
    node_stale: number;
    edge_eligible: number;
    edge_current: number;
    edge_requested: number;
    edge_missing: number;
  };
  feedback_queue?: { raw_count: number; visible_group_count: number; visible_item_count: number };
  graph_correction_patches?: { total: number; proposed_count: number; rejected_count: number };
}

export interface FeedbackQueueResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  group_count: number;
  count: number;
  groups: FeedbackQueueGroup[];
  summary: FeedbackQueueSummary;
  action_catalog?: FeedbackActionCatalog;
}

export interface FeedbackQueueGroup {
  queue_id: string;
  group_by: string;
  lane: string;
  category?: string;
  category_label?: string;
  action_hint?: string;
  priority?: string;
  source_node_ids?: string[];
  target_type: "node" | "edge" | string;
  target_id: string;
  issue_type?: string;
  target_ids?: string[];
  target_count?: number;
  representative_feedback_id: string;
  representative_issue: string;
  feedback_ids: string[];
  item_count: number;
  suppressed_count?: number;
  active_claim_count?: number;
  claim?: Record<string, unknown>;
  semantic_review_ready?: boolean;
  semantic_review_gate?: {
    ready?: boolean;
    reason?: string;
    source_node_ids?: string[];
    statuses?: Record<string, {
      status?: string;
      feature_hash?: string;
      has_file_hashes?: boolean;
      updated_at?: string;
    }>;
    missing_node_ids?: string[];
    pending_node_ids?: string[];
    stale_node_ids?: string[];
  };
  requires_human_signoff?: boolean;
  confidence?: number;
  created_at?: string;
  updated_at?: string;
  graph_structure_lifecycle?: {
    operation_type?: string;
    subtype?: string;
    subtype_label?: string;
    changed_files?: string[];
    file_count?: number;
    requires_commit?: boolean;
    update_graph_after_commit?: boolean;
    semantic_lifecycle?: string;
    reasons?: string[];
    evidence?: Array<{
      feedback_id?: string;
      issue_type?: string;
      reason?: string;
      intent?: string;
      subtype?: string;
      paths?: string[];
    }>;
    supported_actions?: string[];
    message?: string;
  };
}

export interface FeedbackQueueSummary {
  raw_count: number;
  visible_group_count: number;
  visible_item_count: number;
  hidden_status_observation_count: number;
  hidden_resolved_count: number;
  hidden_claimed_count: number;
  hidden_semantic_pending_count: number;
  require_current_semantic: boolean;
  by_kind: Record<string, number>;
  by_status: Record<string, number>;
  by_lane_all_items: Record<string, number>;
  by_lane_visible_groups: Record<string, number>;
  by_category_all_items?: Record<string, number>;
  by_category_visible_groups?: Record<string, number>;
}

export interface FeedbackActionCatalog {
  lanes?: Record<string, FeedbackActionCatalogEntry | string>;
  categories?: Record<string, FeedbackActionCatalogEntry | string>;
  category_order?: string[];
  category_labels?: Record<string, string>;
  decision_actions?: string[];
  review_decisions?: string[];
  status_observation_categories?: string[];
  endpoints?: Record<string, string>;
}

export interface FeedbackActionCatalogEntry {
  label?: string;
  description?: string;
  primary_actions?: string[];
}

export type AssetImpactResolutionKind = "updated" | "keep_unchanged" | "waived";

export interface AssetImpactRemindersResponse {
  ok?: boolean;
  project_id: string;
  status?: string;
  asset_kind?: string;
  reminders?: AssetImpactReminder[];
  items?: AssetImpactReminder[];
  count?: number;
  summary?: AssetImpactReminderSummary;
  unavailable?: boolean;
  error?: string;
  request_id?: string;
}

export interface AssetImpactReminderSummary {
  by_kind?: Record<string, number>;
  by_asset_kind?: Record<string, number>;
  by_status?: Record<string, number>;
  total?: number;
  pending?: number;
}

export interface AssetImpactReminder {
  project_id?: string;
  reminder_id: string;
  impact_key?: string;
  asset_kind: "doc" | "test" | "config" | string;
  asset_path: string;
  node_id: string;
  node_title?: string;
  status: "pending" | string;
  lane?: string;
  category?: string;
  category_label?: string;
  first_commit_sha?: string;
  latest_commit_sha?: string;
  first_event_id?: number;
  latest_event_id?: number;
  impact_count?: number;
  open_event_ids?: number[];
  evidence?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export interface AssetImpactReminderEventsResponse {
  ok?: boolean;
  project_id: string;
  reminder_id?: string;
  reminder?: AssetImpactReminder;
  events: AssetImpactEvent[];
  count?: number;
  request_id?: string;
}

export interface AssetImpactEvent {
  id?: number;
  project_id?: string;
  event_type: "impact_detected" | "resolution_recorded" | string;
  asset_kind?: string;
  asset_path?: string;
  node_id?: string;
  node_title?: string;
  commit_sha?: string;
  snapshot_id?: string;
  run_id?: string;
  actor?: string;
  status?: string;
  impact_key?: string;
  covers_event_ids?: number[];
  evidence?: Record<string, unknown>;
  created_at?: string;
}

export interface AssetImpactReminderResolveResponse {
  ok?: boolean;
  project_id?: string;
  reminder_id?: string;
  resolution?: {
    resolution_kind?: AssetImpactResolutionKind | string;
    actor?: string;
    note?: string;
    covers_event_ids?: number[];
  };
  resolution_kind?: AssetImpactResolutionKind | string;
  reminder?: AssetImpactReminder;
  event?: AssetImpactEvent;
  events?: AssetImpactEvent[];
  covers_event_ids?: number[];
  projection?: Record<string, unknown>;
  request_id?: string;
}

export interface BacklogResponse {
  bugs: BacklogBug[];
  count: number;
  total_count?: number;
  filtered_count?: number;
  view?: "compact" | "full" | string;
  limit?: number | null;
  offset?: number;
  has_more?: boolean;
  next_offset?: number | null;
  truncated?: boolean;
  summary?: BacklogSummary;
  request_id?: string;
}

export interface BacklogSummary {
  total: number;
  open: number;
  fixed: number;
  urgent_open: number;
  by_status: Record<string, number>;
  by_priority: Record<string, number>;
}

export interface BacklogBug {
  bug_id: string;
  title: string;
  status: string;
  priority: "P0" | "P1" | "P2" | "P3" | string;
  target_files?: string[] | string;
  test_files?: string[] | string;
  acceptance_criteria?: string[] | string;
  details_md?: string;
  details_preview?: string;
  commit?: string;
  chain_trigger_json?: Record<string, unknown> | string;
  bypass_policy_json?: string;
  bypass_policy?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
  fixed_at?: string;
  required_docs?: string[];
  provenance_paths?: string[];
  chain_stage?: string;
  runtime_state?: string;
  current_task_id?: string;
  root_task_id?: string;
  worktree_branch?: string;
  worktree_path?: string;
  mf_type?: string;
  source_raw_id?: string;
  original_request_excerpt?: string;
  original_request_source?: string;
  original_request_missing?: boolean;
  original_request_missing_reason?: string;
  target_file_count?: number;
  test_file_count?: number;
  acceptance_count?: number;
  required_doc_count?: number;
  provenance_count?: number;
  contract_summary?: BacklogContractSummary;
  compact?: boolean;
}

export interface BacklogContractSummary {
  has_contract?: boolean;
  template_id?: string;
  contract_instance_id?: string;
  required_evidence_count?: number;
  optional_evidence_count?: number;
}

export interface TaskTimelineResponse {
  ok?: boolean;
  project_id: string;
  task_id?: string;
  backlog_id: string;
  trace_id?: string;
  events: TaskTimelineEvent[];
  count: number;
  request_id?: string;
}

export interface BacklogTimelineGateResponse {
  ok?: boolean;
  project_id: string;
  bug_id: string;
  applicable: boolean;
  reason?: string;
  can_close: boolean;
  timeline_gate: MfCloseTimelineGate;
  event_count: number;
  events?: TaskTimelineEvent[];
  request_id?: string;
}

export interface MfCloseTimelineGate {
  schema_version?: string;
  passed?: boolean;
  status?: string;
  required_event_kinds?: string[];
  present_event_kinds?: string[];
  missing_event_kinds?: string[];
  event_count?: number;
  ignored_required_events?: Record<string, unknown>[];
  contract_gate?: MfContractGate;
  route_context_gate?: MfRouteContextGate;
  missing_evidence_groups?: MfMissingEvidenceGroups;
  route_context_reminder?: MfRouteContextReminder;
  checks?: Record<string, boolean | number | string>;
}

export interface MfContractGate {
  schema_version?: string;
  passed?: boolean;
  status?: string;
  template_id?: string;
  contract_instance_id?: string;
  required_requirement_ids?: string[];
  optional_requirement_ids?: string[];
  present_requirement_ids?: string[];
  missing_requirement_ids?: string[];
  evidence_events?: Record<string, unknown>[];
  checks?: Record<string, boolean | number | string>;
}

export interface MfRouteContextGate {
  schema_version?: string;
  passed?: boolean;
  status?: string;
  required?: boolean;
  required_requirement_ids?: string[];
  present_requirement_ids?: string[];
  missing_requirement_ids?: string[];
  topology_policy?: Record<string, unknown>;
  route_identity?: Record<string, unknown>;
  same_route_identity?: boolean;
  evidence_events?: Record<string, unknown>;
  ignored_route_events?: Record<string, unknown>[];
  checks?: Record<string, boolean | number | string>;
}

export interface MfMissingEvidenceGroups {
  schema_version?: string;
  groups?: Record<string, MfMissingEvidenceGroup>;
}

export interface MfMissingEvidenceGroup {
  label?: string;
  missing?: string[];
  next_action?: string;
  next_actions?: string[];
}

export interface MfRouteContextReminder {
  schema_version?: string;
  required?: boolean;
  blocked?: boolean;
  status?: string;
  contract_template_id?: string;
  allowed_stages?: string[];
  selected_topology?: string;
  recommended_topology?: string;
  priority?: string;
  next_actions?: Record<string, unknown>[];
  missing_evidence_groups?: Record<string, MfMissingEvidenceGroup>;
  identity_recovery?: Record<string, unknown>;
  boundary?: Record<string, unknown>;
}

export interface TaskTimelineEvent {
  id?: number;
  event_id?: string;
  project_id?: string;
  backlog_id?: string;
  mf_id?: string;
  task_id?: string;
  attempt_num?: number;
  event_type: string;
  phase?: string;
  event_kind?: string;
  scenario_id?: string;
  correlation_id?: string;
  severity?: string;
  decision?: string;
  actor?: string;
  status?: string;
  payload?: Record<string, unknown>;
  verification?: TaskTimelineVerification | Record<string, unknown>;
  artifact_refs?: Record<string, unknown>;
  trace_id?: string;
  commit_sha?: string;
  created_at?: string;
}

export interface TaskTimelineVerification {
  passed?: boolean;
  status?: string;
  warnings?: string[];
  errors?: string[];
  checks?: Record<string, boolean>;
  [key: string]: unknown;
}

export interface ContentSysDemoVisualizationEvidence {
  schema_version?: string;
  artifact_id?: string;
  fixture_id?: string;
  scenario_id?: string;
  public_summary?: string;
  route_identity?: Record<string, unknown>;
  route_refs?: Record<string, unknown>;
  status_cards?: Record<string, unknown>[];
  timeline_events?: Record<string, unknown>[];
  artifact_refs?: Record<string, unknown>[];
  privacy_boundary?: Record<string, unknown>;
  frontend_display_contract?: Record<string, unknown>;
  [key: string]: unknown;
}

export type AssetInboxStatus =
  | "source_orphan"
  | "doc_unbound"
  | "doc_candidate"
  | "accepted"
  | "test_candidate"
  | "config_pending_decision"
  | "ignored"
  | "archive"
  | "stale"
  | "impact_pending"
  | "drift_suspected"
  | "drift_confirmed"
  | "drift_resolved"
  | "drift_waived";

export type AssetInboxKind =
  | "source"
  | "doc"
  | "index_doc"
  | "test"
  | "config"
  | "generated"
  | "ignored"
  | "other"
  | "unknown";

export type AssetInboxBatchActionName =
  | "queue_asset_binding_proposals"
  | "queue_semantic_enrich"
  | "reject_or_waive_candidates"
  | "create_backlog_from_selection"
  | "write_governance_hint"
  | "propose_remove_binding"
  | "resolve_drift"
  | "mark_drift_state";

export interface AssetInboxResponse {
  schema_version: "asset_inbox.v1" | string;
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  commit_sha: string;
  generated_at?: string;
  source_artifacts?: Record<string, string>;
  impact_scope_policy: "accepted_bindings_only" | string;
  backlog_policy: AssetInboxBacklogPolicy;
  summary: AssetInboxSummary;
  items: AssetInboxItem[];
  asset_groups?: AssetInboxAssetGroup[];
  batch_actions: AssetInboxBatchAction[];
  precheck?: AssetInboxResponsePrecheck;
}

export interface AssetInboxBacklogPolicy {
  default_container: false;
  create_from_selected_assets_only: true;
  reason?: string;
}

export interface AssetInboxSummary {
  total: number;
  by_status: Record<AssetInboxStatus | string, number>;
  by_kind?: Record<AssetInboxKind | string, number>;
  candidate_count?: number;
  accepted_count?: number;
  unbound_count?: number;
  backlog_eligible_count?: number;
  operator_review_count?: number;
}

export interface AssetInboxItem {
  asset_id: string;
  path: string;
  asset_kind: AssetInboxKind | string;
  language?: string;
  asset_status: AssetInboxStatus | string;
  scan_status?: string;
  graph_status?: string;
  doc_kind?: string;
  binding_status?: string;
  file_hash: string;
  sha256?: string;
  size_bytes?: number;
  accepted_bindings: AssetInboxBinding[];
  binding_candidates: AssetInboxBindingCandidate[];
  mount_relations?: AssetInboxMountRelation[];
  relation_summary?: AssetInboxRelationSummary;
  drift?: AssetInboxDriftState;
  drift_proposal?: AssetInboxDriftProposal;
  recommended_actions: Array<AssetInboxBatchActionName | string>;
  batch_eligible_actions?: Array<AssetInboxBatchActionName | string>;
  risk?: "low" | "medium" | "high" | string;
  evidence: AssetInboxEvidence[];
  backlog: AssetInboxBacklogState;
}

export interface AssetInboxAssetGroup {
  group_id?: "doc" | "test" | "config" | "source" | "generated" | "ignored" | "other" | string;
  group?: "doc" | "test" | "config" | "source" | "generated" | "ignored" | "other" | string;
  label?: string;
  count: number;
  item_ids?: string[];
  items?: AssetInboxAssetGroupItem[];
  paths?: string[];
  status_counts?: Record<AssetInboxStatus | string, number>;
  statuses?: Record<AssetInboxStatus | string, number>;
}

export interface AssetInboxAssetGroupItem {
  asset_id: string;
  path: string;
  asset_status: AssetInboxStatus | string;
  asset_kind: AssetInboxKind | string;
  relation_count?: number;
  review_required_count?: number;
  impact_scope_count?: number;
}

export interface AssetInboxMountRelation {
  relation_id: string;
  status: "accepted" | "candidate" | "unbound" | "stale_drift" | "impact_pending" | string;
  role?: "doc" | "test" | "config" | string;
  target_node_id: string;
  target_title?: string;
  source?: string;
  evidence_kind?: string;
  proposal_hash?: string;
  binding_strength?: "weak" | "strong" | string;
  impact_scope?: boolean | string | string[];
  review_required?: boolean;
  impact_reminder_id?: string;
  drift_state?: AssetInboxDriftStateName | string;
}

export type AssetInboxDriftStateName = "not_drifted" | "suspected" | "confirmed" | "resolved" | "waived";

export interface AssetInboxDriftState {
  schema_version?: "asset_drift_state.v1" | string;
  state: AssetInboxDriftStateName | string;
  source?: string;
  actor?: string;
  evidence?: Record<string, unknown>;
  impact_pending?: boolean;
  impact_reminders?: Array<{
    reminder_id: string;
    node_id?: string;
    node_title?: string;
    impact_count?: number;
    latest_commit_sha?: string;
  }>;
}

export interface AssetInboxDriftProposal {
  proposal_id?: string;
  status?: string;
  ai_status?: string;
  node_id?: string;
  self_precheck?: Record<string, unknown>;
  evidence?: Record<string, unknown>;
  updated_at?: string;
}

export interface AssetInboxRelationSummary {
  accepted_count?: number;
  candidate_count?: number;
  relation_count?: number;
  impact_scope_count?: number;
  review_required_count?: number;
}

export interface AssetInboxBinding {
  node_id: string;
  title?: string;
  role: "doc" | "test" | "config" | string;
  source: string;
}

export interface AssetInboxBindingCandidate {
  schema_version: "asset_binding_proposal.v1" | string;
  operation: string;
  asset_kind: AssetInboxKind | string;
  asset_path: string;
  target_node_id: string;
  target_title?: string;
  evidence_kind: string;
  source: string;
  proposal_hash: string;
  precheck: AssetInboxPrecheck;
}

export interface AssetInboxPrecheck {
  schema_version: "asset_binding_precheck.v1" | string;
  ok: boolean;
  mode?: string;
  decision?: "review_required" | "accepted" | "rejected" | string;
  binding_strength?: "weak" | "strong" | string;
  proposal_hash?: string;
  errors?: string[];
  warnings?: string[];
}

export interface AssetInboxEvidence {
  kind: string;
  message: string;
  [key: string]: unknown;
}

export interface AssetInboxBacklogState {
  eligible: boolean;
  reason?: string;
}

export interface AssetInboxBatchAction {
  action: AssetInboxBatchActionName | string;
  label?: string;
  allowed_statuses: Array<AssetInboxStatus | string>;
  requires_selection?: boolean;
  requires_review?: boolean;
  mutates_source?: boolean;
  creates_backlog?: boolean;
  payload_example?: Record<string, unknown>;
}

export interface AssetInboxResponsePrecheck {
  schema_version?: string;
  ok?: boolean;
  errors?: string[];
  warnings?: string[];
  status_count?: number;
  item_count?: number;
}

export interface FileInventoryRow {
  path: string;
  file_kind?: string;
  language?: string;
  scan_status?: string;
  graph_status?: string;
  decision?: string;
  candidate_node_id?: string;
  attached_node_ids?: string[];
  mapped_node_ids?: string[];
  attachment_role?: string;
  attachment_source?: string;
  size_bytes?: number;
  reason?: string;
}

export interface SnapshotFilesResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  summary?: Record<string, unknown>;
  total_count: number;
  filtered_count: number;
  sort?: string;
  files: FileInventoryRow[];
}

export interface AttachFileHintResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  path: string;
  target_node_id: string;
  role: "doc" | "test" | "config" | string;
  state: "written_uncommitted" | string;
  hint_written: boolean;
  already_present?: boolean;
  requires_commit: boolean;
  update_graph_after_commit: boolean;
  message?: string;
  hint?: string;
  file?: FileInventoryRow;
  target_node?: NodeRecord;
}

export interface UnbindFileHintResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  path: string;
  target_node_id: string;
  role: "doc" | "test" | "config" | string;
  state?: "written_uncommitted" | "planned" | string;
  written_uncommitted?: boolean;
  requires_commit?: boolean;
  update_graph_after_commit?: boolean;
  source_controlled?: boolean;
  operation_type?: string;
  changed_files?: string[];
  message?: string;
  review_queue?: {
    queued?: boolean;
    feedback?: Record<string, unknown> | Record<string, unknown>[];
    operation_type?: string;
    subtype?: string;
  };
  file?: FileInventoryRow;
  target_node?: NodeRecord;
}

export interface AssetDriftStateResponse {
  ok: boolean;
  project_id: string;
  schema_version: string;
  drift_state: Record<string, unknown>;
}

export interface AssetDriftProposalResponse {
  ok: boolean;
  project_id: string;
  schema_version: string;
  ai_available: boolean;
  ai_reason: string;
  proposal: AssetInboxDriftProposal & Record<string, unknown>;
}

export interface FileHygieneActionResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  action: string;
  event: Record<string, unknown>;
  message?: string;
  review_queue?: {
    queued?: boolean;
    feedback?: Record<string, unknown> | Record<string, unknown>[];
    operation_type?: string;
    subtype?: string;
    changed_files?: string[];
  };
  file?: FileInventoryRow;
}
