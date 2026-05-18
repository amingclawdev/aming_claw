"""Graph-structure rule fingerprints.

Graph snapshots are derived state.  When the graph-building rules change, the
snapshot should be treated as stale and rebuilt instead of trying to roll back
individual rows in the graph DB.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from agent.governance.graph_structure_hints import load_graph_structure_hints
from agent.governance.reconcile_semantic_config import DEFAULT_CONFIG_PATH, PROJECT_OVERRIDE_PATH


SCHEMA_VERSION = 1

ALGORITHM_INPUT_PATHS: tuple[str, ...] = (
    "agent/governance/reconcile_phases/phase_z_v2.py",
    "agent/governance/reconcile_file_inventory.py",
    "agent/governance/reconcile_semantic_config.py",
    "agent/governance/graph_hint_projection.py",
    "agent/governance/graph_structure_hints.py",
    "agent/governance/semantic_graph_structure_bridge.py",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _json_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _file_record(path: Path, *, label: str) -> dict[str, Any]:
    try:
        digest = _file_hash(path) if path.exists() and path.is_file() else ""
    except OSError:
        digest = ""
    return {
        "path": label.replace("\\", "/"),
        "sha256": digest,
        "missing": not bool(digest),
    }


def _component_hash(records: list[dict[str, Any]]) -> str:
    return _json_hash(records)


def _algorithm_component() -> dict[str, Any]:
    root = _repo_root()
    records = [
        _file_record(root / rel, label=rel)
        for rel in ALGORITHM_INPUT_PATHS
    ]
    return {
        "fingerprint": _component_hash(records),
        "files": records,
    }


def _semantic_config_component(project_root: Path, semantic_config_path: str | Path | None) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    explicit = Path(semantic_config_path) if semantic_config_path else None
    if explicit is not None:
        path = explicit if explicit.is_absolute() else project_root / explicit
        label = explicit.as_posix()
        try:
            label = path.relative_to(project_root).as_posix()
        except ValueError:
            label = f"external:{path.name}"
        records.append(_file_record(path, label=label))
    else:
        default_path = Path(DEFAULT_CONFIG_PATH)
        records.append(_file_record(default_path, label="aming-claw-default:config/reconcile/semantic_enrichment.yaml"))

    override_path = project_root / PROJECT_OVERRIDE_PATH
    records.append(_file_record(override_path, label=PROJECT_OVERRIDE_PATH.as_posix()))
    return {
        "fingerprint": _component_hash(records),
        "files": records,
    }


def _hint_component(project_root: Path, hint_index: Mapping[str, Any] | None) -> dict[str, Any]:
    if hint_index is None:
        try:
            hint_index = load_graph_structure_hints(project_root)
        except Exception as exc:
            return {
                "fingerprint": _json_hash({"error": type(exc).__name__}),
                "hint_count": 0,
                "hints": [],
                "error": type(exc).__name__,
            }
    hints = [
        {
            "hint_id": str(item.get("hint_id") or ""),
            "op": str(item.get("op") or ""),
            "source_path": str(item.get("source_path") or ""),
            "target_node_id": str(item.get("target_node_id") or ""),
            "edge": str(item.get("edge") or ""),
            "role": str(item.get("role") or ""),
            "anchor": item.get("anchor") if isinstance(item.get("anchor"), dict) else {},
        }
        for item in (hint_index.get("hints") or [])
        if isinstance(item, Mapping)
    ]
    hints.sort(key=lambda item: (item["source_path"], item["hint_id"], item["op"]))
    return {
        "fingerprint": _json_hash(hints),
        "hint_count": len(hints),
        "hints": hints,
    }


def build_graph_rule_fingerprint(
    project_root: str | Path,
    *,
    commit_sha: str = "",
    hint_index: Mapping[str, Any] | None = None,
    semantic_config_path: str | Path | None = None,
    include_source_hints: bool = True,
) -> dict[str, Any]:
    """Return the graph-rule fingerprint for the current rule inputs."""
    root = Path(project_root).resolve()
    components = {
        "algorithm": _algorithm_component(),
        "semantic_enrichment_config": _semantic_config_component(root, semantic_config_path),
    }
    if include_source_hints:
        components["source_hints"] = _hint_component(root, hint_index)
    else:
        components["source_hints"] = {
            "fingerprint": "",
            "hint_count": 0,
            "hints": [],
            "skipped": True,
            "skip_reason": "commit_drift_covers_source_hint_changes",
        }
    rebuild_components = {
        key: components[key]
        for key in ("algorithm", "semantic_enrichment_config")
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "fingerprint": _json_hash({
            "schema_version": SCHEMA_VERSION,
            "components": components,
        }),
        "rebuild_input_fingerprint": _json_hash({
            "schema_version": SCHEMA_VERSION,
            "components": rebuild_components,
        }),
        "commit_sha": str(commit_sha or ""),
        "components": components,
        "rebuild_required_on_mismatch": True,
        "recommended_action": "run_full_reconcile",
    }


def snapshot_rule_fingerprint(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {}
    raw_notes = snapshot.get("notes")
    try:
        notes = json.loads(str(raw_notes or "{}"))
    except (TypeError, ValueError):
        return {}
    if not isinstance(notes, dict):
        return {}
    raw = notes.get("graph_rule_fingerprint")
    return raw if isinstance(raw, dict) else {}


def compact_rule_fingerprint(fingerprint: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = fingerprint or {}
    raw_components = raw.get("components") if isinstance(raw.get("components"), Mapping) else {}
    components: dict[str, Any] = {}
    for name, value in raw_components.items():
        if not isinstance(value, Mapping):
            continue
        files = value.get("files") if isinstance(value.get("files"), list) else []
        missing_count = sum(
            1 for item in files
            if isinstance(item, Mapping) and bool(item.get("missing"))
        )
        entry: dict[str, Any] = {
            "fingerprint": str(value.get("fingerprint") or ""),
        }
        if files:
            entry["file_count"] = len(files)
            entry["missing_file_count"] = missing_count
        if "hint_count" in value:
            entry["hint_count"] = int(value.get("hint_count") or 0)
        if bool(value.get("skipped")):
            entry["skipped"] = True
            entry["skip_reason"] = str(value.get("skip_reason") or "")
        components[str(name)] = entry
    return {
        "schema_version": int(raw.get("schema_version") or SCHEMA_VERSION),
        "fingerprint": str(raw.get("fingerprint") or ""),
        "rebuild_input_fingerprint": str(raw.get("rebuild_input_fingerprint") or ""),
        "commit_sha": str(raw.get("commit_sha") or ""),
        "components": components,
        "rebuild_required_on_mismatch": bool(raw.get("rebuild_required_on_mismatch")),
        "recommended_action": str(raw.get("recommended_action") or ""),
    }


def compare_rule_fingerprint(
    snapshot_fingerprint: Mapping[str, Any] | None,
    current_fingerprint: Mapping[str, Any] | None,
) -> dict[str, Any]:
    snapshot_hash = str(
        (snapshot_fingerprint or {}).get("rebuild_input_fingerprint")
        or (snapshot_fingerprint or {}).get("fingerprint")
        or ""
    )
    current_hash = str(
        (current_fingerprint or {}).get("rebuild_input_fingerprint")
        or (current_fingerprint or {}).get("fingerprint")
        or ""
    )
    available = bool(snapshot_hash and current_hash)
    mismatch = bool(available and snapshot_hash != current_hash)
    return {
        "available": available,
        "mismatch": mismatch,
        "snapshot_fingerprint": snapshot_hash,
        "current_fingerprint": current_hash,
        "snapshot_full_fingerprint": str((snapshot_fingerprint or {}).get("fingerprint") or ""),
        "current_full_fingerprint": str((current_fingerprint or {}).get("fingerprint") or ""),
        "snapshot": compact_rule_fingerprint(snapshot_fingerprint),
        "current": compact_rule_fingerprint(current_fingerprint),
        "recommended_action": "run_full_reconcile" if mismatch else "",
    }


def build_full_reconcile_anchor(
    *,
    project_id: str,
    snapshot_id: str,
    anchor_commit: str,
    rule_fingerprint: Mapping[str, Any],
    reconcile_mode: str = "full",
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "anchor_commit": anchor_commit,
        "structure_rule_fingerprint": str(rule_fingerprint.get("fingerprint") or ""),
        "rebuild_input_fingerprint": str(rule_fingerprint.get("rebuild_input_fingerprint") or ""),
        "reconcile_mode": reconcile_mode,
        "rule_schema_version": int(rule_fingerprint.get("schema_version") or SCHEMA_VERSION),
    }


__all__ = [
    "ALGORITHM_INPUT_PATHS",
    "SCHEMA_VERSION",
    "build_full_reconcile_anchor",
    "build_graph_rule_fingerprint",
    "compact_rule_fingerprint",
    "compare_rule_fingerprint",
    "snapshot_rule_fingerprint",
]
