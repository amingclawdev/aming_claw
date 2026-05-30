"""Policy-based impact analysis.

Given file changes, determines which nodes need re-verification,
what tests to run, and in what order.

Also provides code→doc relationship inference: when code files change,
affected documentation files are surfaced so gates can enforce doc updates.
"""

import os
from pathlib import Path

from .enums import VerifyStatus, VerifyLevel
from .models import FileHitPolicy, PropagationPolicy, VerificationPolicy, ImpactAnalysisRequest

# Code path prefix → related documentation files
# Used by checkpoint gate to verify doc consistency on code changes
CODE_DOC_MAP = {
    "agent/telegram_gateway/": [
        "docs/architecture.md",
        "README.md",
    ],
    "agent/governance/server.py": [
        "docs/api/governance-api.md",
        "docs/architecture.md",
        "README.md",
    ],
    "agent/governance/auto_chain.py": [
        "docs/governance/auto-chain.md",
        "docs/governance/gates.md",
    ],
    "agent/governance/task_registry.py": [
        "docs/api/governance-api.md",
        "README.md",
    ],
    "agent/governance/state_service.py": [
        "docs/governance/acceptance-graph.md",
        "docs/governance/version-control.md",
    ],
    "agent/governance/role_service.py": [
        "docs/config/role-permissions.md",
        "docs/roles/README.md",
    ],
    "agent/governance/gatekeeper.py": [
        "docs/governance/gates.md",
    ],
    "agent/executor_api.py": [
        "docs/api/executor-api.md",
    ],
    "agent/ai_lifecycle.py": [
        "docs/architecture.md",
    ],
    "agent/deploy_chain.py": [
        "docs/deployment.md",
    ],
    "agent/service_manager.py": [
        "docs/architecture.md",
    ],
    "agent/executor_worker.py": [
        "docs/api/executor-api.md",
    ],
    "agent/governance/memory_backend.py": [
        "docs/governance/memory.md",
    ],
    "agent/governance/memory_service.py": [
        "docs/governance/memory.md",
    ],
    "agent/governance/conflict_rules.py": [
        "docs/governance/conflict-rules.md",
    ],
    "agent/governance/chain_context.py": [
        "docs/governance/auto-chain.md",
    ],
    "agent/governance/db.py": [
        "docs/architecture.md",
    ],
    "agent/governance/impact_analyzer.py": [
        "docs/governance/acceptance-graph.md",
    ],
    "agent/governance/observability.py": [
        "docs/architecture.md",
    ],
    "agent/governance/preflight.py": [
        "docs/governance/acceptance-graph.md",
    ],
    "agent/governance/gate_policy.py": [
        "docs/governance/gates.md",
    ],
    "agent/governance/graph.py": [
        "docs/governance/acceptance-graph.md",
    ],
    "agent/role_permissions.py": [
        "docs/config/role-permissions.md",
    ],
    # --- Additions below: cover all agent/**/*.py modules >30 significant lines ---
    # Governance modules
    "agent/governance/redeploy_handler.py": [
        "docs/api/governance-api.md",
    ],
    "agent/governance/artifacts.py": [
        "docs/api/governance-api.md",
        "docs/governance/acceptance-graph.md",
    ],
    "agent/governance/doc_generator.py": [
        "docs/api/governance-api.md",
    ],
    "agent/governance/audit_service.py": [
        "docs/architecture.md",
    ],
    "agent/governance/evidence.py": [
        "docs/governance/acceptance-graph.md",
    ],
    "agent/governance/event_bus.py": [
        "docs/architecture.md",
    ],
    "agent/governance/session_context.py": [
        "docs/architecture.md",
    ],
    "agent/governance/drift_detector.py": [
        "docs/governance/acceptance-graph.md",
    ],
    "agent/governance/doc_policy.py": [
        "docs/governance/gates.md",
    ],
    "agent/governance/project_service.py": [
        "docs/api/governance-api.md",
    ],
    "agent/governance/reconcile.py": [
        "docs/governance/auto-chain.md",
    ],
    "agent/governance/mcp_server.py": [
        "docs/api/governance-api.md",
    ],
    "agent/governance/failure_classifier.py": [
        "docs/governance/auto-chain.md",
    ],
    "agent/governance/client.py": [
        "docs/api/governance-api.md",
    ],
    "agent/governance/coverage_check.py": [
        "docs/governance/acceptance-graph.md",
    ],
    "agent/governance/graph_generator.py": [
        "docs/governance/acceptance-graph.md",
    ],
    "agent/governance/token_service.py": [
        "docs/api/governance-api.md",
    ],
    "agent/governance/redis_client.py": [
        "docs/architecture.md",
    ],
    "agent/governance/agent_lifecycle.py": [
        "docs/architecture.md",
    ],
    "agent/governance/enums.py": [
        "docs/governance/acceptance-graph.md",
    ],
    "agent/governance/errors.py": [
        "docs/governance/acceptance-graph.md",
    ],
    "agent/governance/idempotency.py": [
        "docs/api/governance-api.md",
    ],
    "agent/governance/llm_utils.py": [
        "docs/architecture.md",
    ],
    "agent/governance/models.py": [
        "docs/governance/acceptance-graph.md",
    ],
    "agent/governance/outbox.py": [
        "docs/architecture.md",
    ],
    "agent/governance/permissions.py": [
        "docs/config/role-permissions.md",
    ],
    "agent/governance/role_config.py": [
        "docs/config/role-permissions.md",
    ],
    "agent/governance/session_persistence.py": [
        "docs/architecture.md",
    ],
    # MCP modules
    "agent/mcp/server.py": [
        "docs/architecture.md",
    ],
    "agent/mcp/executor.py": [
        "docs/architecture.md",
    ],
    "agent/mcp/tools.py": [
        "docs/architecture.md",
    ],
    "agent/mcp/events.py": [
        "docs/architecture.md",
    ],
    # Main agent modules
    "agent/manager_http_server.py": [
        "docs/api/governance-api.md",
    ],
    "agent/_patch_locales.py": [
        "docs/architecture.md",
    ],
    "agent/ai_output_parser.py": [
        "docs/architecture.md",
    ],
    "agent/backends.py": [
        "docs/architecture.md",
    ],
    "agent/cli.py": [
        "docs/architecture.md",
    ],
    "agent/config.py": [
        "docs/architecture.md",
    ],
    "agent/context_assembler.py": [
        "docs/architecture.md",
    ],
    "agent/context_store.py": [
        "docs/architecture.md",
    ],
    "agent/decision_validator.py": [
        "docs/governance/gates.md",
    ],
    "agent/evidence_collector.py": [
        "docs/governance/acceptance-graph.md",
    ],
    "agent/execution_sandbox.py": [
        "docs/architecture.md",
    ],
    "agent/executor.py": [
        "docs/api/executor-api.md",
    ],
    "agent/graph_validator.py": [
        "docs/governance/acceptance-graph.md",
    ],
    "agent/i18n.py": [
        "docs/architecture.md",
    ],
    "agent/memory_write_guard.py": [
        "docs/governance/memory.md",
    ],
    "agent/notification_gateway.py": [
        "docs/architecture.md",
    ],
    "agent/observability.py": [
        "docs/architecture.md",
    ],
    "agent/pipeline_config.py": [
        "docs/architecture.md",
    ],
    "agent/project_config.py": [
        "docs/architecture.md",
    ],
    "agent/task_orchestrator.py": [
        "docs/architecture.md",
    ],
    "agent/task_state_machine.py": [
        "docs/governance/acceptance-graph.md",
    ],
    "agent/utils.py": [
        "docs/architecture.md",
    ],
    "agent/workspace_queue.py": [
        "docs/architecture.md",
    ],
    # Telegram gateway submodules (supplement directory prefix entry)
    "agent/telegram_gateway/chat_proxy.py": [
        "docs/architecture.md",
    ],
    "agent/telegram_gateway/gateway.py": [
        "docs/architecture.md",
    ],
    "agent/telegram_gateway/gov_event_listener.py": [
        "docs/architecture.md",
    ],
    "agent/telegram_gateway/message_worker.py": [
        "docs/architecture.md",
    ],
}


def get_related_docs(changed_files: list[str]) -> set[str]:
    """Given code file changes, return set of docs that may need updating."""
    docs = set()
    for cf in changed_files:
        for pattern, doc_list in CODE_DOC_MAP.items():
            if pattern in cf or cf == pattern:
                docs.update(doc_list)
    return docs


def _load_project_code_doc_map(
    project_id: str = None,
    *,
    project_root: str | os.PathLike | None = None,
    fallback_to_builtin: bool = True,
) -> dict:
    """Load project-specific code_doc_map.json if available, else return CODE_DOC_MAP.

    When code_doc_map.json exists in the project's governance data dir,
    it is used instead of the hardcoded CODE_DOC_MAP (R6, AC5).
    """
    candidates = []
    if project_id:
        try:
            # Try to find governance data dir
            try:
                from .db import _governance_root
                candidates.append(_governance_root() / project_id / "code_doc_map.json")
            except Exception:
                pass
        except Exception:
            pass
    if project_root:
        root = Path(project_root).resolve()
        candidates.extend([
            root / "code_doc_map.json",
            root / ".aming-claw" / "code_doc_map.json",
        ])
    for cdm_path in candidates:
        try:
            if cdm_path and cdm_path.exists():
                import json as _json
                with open(str(cdm_path), "r", encoding="utf-8") as f:
                    payload = _json.load(f)
                return payload if isinstance(payload, dict) else {}
        except Exception:
            pass
    return CODE_DOC_MAP if fallback_to_builtin else {}


class ImpactAnalyzer:
    """Analyzes the impact of file changes on the acceptance graph."""

    def __init__(self, graph, get_node_status_fn, project_id: str = None):
        """
        Args:
            graph: AcceptanceGraph instance.
            get_node_status_fn: callable(node_id) -> VerifyStatus
            project_id: Optional project ID for loading project-specific code_doc_map.
        """
        self.graph = graph
        self.get_status = get_node_status_fn
        self.project_id = project_id
        self._code_doc_map = None

    @property
    def code_doc_map(self):
        if self._code_doc_map is None:
            self._code_doc_map = _load_project_code_doc_map(self.project_id)
        return self._code_doc_map

    def analyze(self, request: ImpactAnalysisRequest) -> dict:
        file_policy = request.file_policy or FileHitPolicy()
        prop_policy = request.propagation_policy or PropagationPolicy()
        ver_policy = request.verification_policy or VerificationPolicy()

        # Step 1: File → direct hit nodes
        direct_hit = self._file_match(request.changed_files, file_policy)

        # Step 2: Propagation
        affected = set(direct_hit)
        if prop_policy.follow_deps:
            for nid in list(direct_hit):
                try:
                    affected |= self.graph.descendants(nid)
                except Exception:
                    pass

        # Step 3: Pruning
        skipped = []
        if ver_policy.skip_already_passed:
            for nid in list(affected):
                try:
                    status = self.get_status(nid)
                    if status == VerifyStatus.QA_PASS and nid not in direct_hit:
                        affected.discard(nid)
                except Exception:
                    pass

        if ver_policy.respect_gates:
            for nid in list(affected):
                try:
                    gates = self.graph.get_gates(nid)
                    for gate in gates:
                        gate_nid = gate.node_id if hasattr(gate, 'node_id') else gate.get("node_id", "")
                        gate_status = self.get_status(gate_nid)
                        if gate_status in (VerifyStatus.FAILED, VerifyStatus.PENDING):
                            affected.discard(nid)
                            skipped.append({"node": nid, "reason": f"gate {gate_nid} is {gate_status.value}"})
                            break
                except Exception:
                    pass

        # Step 4: Group by verify level + topological sort
        by_phase = {"T1": [], "T2": [], "T3": [], "T4": []}
        level_map = {1: "T1", 2: "T2", 3: "T3", 4: "T4", 5: "T4"}

        for nid in affected:
            try:
                node_data = self.graph.get_node(nid)
                vl = node_data.get("verify_level", 1)
                if isinstance(vl, str):
                    try:
                        vl = int(vl)
                    except ValueError:
                        vl = 1
                phase = level_map.get(vl, "T4")
                by_phase[phase].append(nid)
            except Exception:
                by_phase["T4"].append(nid)

        # Topological order filtered to affected
        try:
            topo = self.graph.topological_order()
            ordered = [n for n in topo if n in affected]
        except Exception:
            ordered = sorted(affected)

        # Collect test files
        test_files = set()
        for nid in affected:
            try:
                node_data = self.graph.get_node(nid)
                for tf in node_data.get("test", []):
                    if tf and tf != "TBD" and tf != "[TBD]":
                        test_files.add(tf)
            except Exception:
                pass

        # Max verify level
        max_vl = 1
        for nid in direct_hit:
            try:
                max_vl = max(max_vl, self.graph.max_verify_level(nid))
            except Exception:
                pass

        # Step 5: Doc consistency — which docs should be reviewed
        # Use project-specific code_doc_map if available
        doc_map = self.code_doc_map
        related_docs = set()
        for cf in request.changed_files:
            for pattern, doc_list in doc_map.items():
                if pattern in cf or cf == pattern:
                    related_docs.update(doc_list)

        # Build affected_nodes list with node details (R8: include gate_mode and verify_level)
        affected_nodes = []
        for nid in ordered:
            try:
                nd = self.graph.get_node(nid)
                vl = nd.get("verify_level", 1)
                if isinstance(vl, str):
                    try:
                        vl = int(vl)
                    except ValueError:
                        vl = 1
                affected_nodes.append({
                    "node_id": nid,
                    "title": nd.get("title", ""),
                    "primary": nd.get("primary", []),
                    "verify_level": vl,
                    "gate_mode": nd.get("gate_mode", "auto"),
                    "verify_requires": nd.get("verify_requires", []),
                    "is_direct": nid in direct_hit,
                })
            except Exception:
                affected_nodes.append({"node_id": nid, "is_direct": nid in direct_hit})

        return {
            "direct_hit": sorted(direct_hit),
            "affected_nodes": affected_nodes,
            "total_affected": len(affected),
            "verification_order": ordered,
            "by_phase": {k: sorted(v) for k, v in by_phase.items()},
            "skipped": skipped,
            "test_files": sorted(test_files),
            "max_verify": max_vl,
            "related_docs": sorted(related_docs),
        }

    def _file_match(self, changed_files: list[str], policy: FileHitPolicy) -> set[str]:
        """Match changed files to graph nodes."""
        import fnmatch as _fnmatch

        changed_set = set(changed_files)
        hits = set()

        for nid in self.graph.list_nodes():
            try:
                node_data = self.graph.get_node(nid)
            except Exception:
                continue

            if policy.match_primary:
                primary = set(node_data.get("primary", []))
                if primary & changed_set:
                    hits.add(nid)
                    continue

            if policy.match_secondary:
                secondary = set(node_data.get("secondary", []))
                if secondary & changed_set:
                    hits.add(nid)
                    continue

            if policy.match_config_glob:
                for pattern in policy.match_config_glob:
                    for cf in changed_files:
                        if _fnmatch.fnmatch(cf, pattern):
                            # Check if this file is in any of the node's file lists
                            all_files = set(node_data.get("primary", [])) | set(node_data.get("secondary", []))
                            if cf in all_files:
                                hits.add(nid)

        return hits
