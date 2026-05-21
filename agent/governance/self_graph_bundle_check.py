"""Read-only compatibility checks for the packaged self graph bundle."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SELF_GRAPH_BUNDLE_SCHEMA_VERSION = 1
SUPPORTED_SELF_GRAPH_BUNDLE_MAJOR = 1
SELF_GRAPH_BUNDLE_MANIFEST_REL_PATH = "agent/mcp/resources/self-graph-bundle-manifest.json"
DEFAULT_SELF_GRAPH_BUNDLE_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / SELF_GRAPH_BUNDLE_MANIFEST_REL_PATH
)


def check_self_graph_bundle(
    *,
    manifest_path: str | Path | None = None,
    plugin_root: str | Path | None = None,
    supported_bundle_major: int = SUPPORTED_SELF_GRAPH_BUNDLE_MAJOR,
    emit_update_event: bool = True,
) -> dict[str, Any]:
    """Validate the packaged self graph/semantic bundle manifest.

    The check is intentionally local and deterministic. It never imports the
    bundle into governance state; it only reports whether this runtime can read
    the packaged bundle contract and, for incompatible major versions, emits a
    machine-readable update-reminder event payload.
    """

    root = _resolve_plugin_root(plugin_root)
    manifest_file = _resolve_manifest_path(manifest_path, root)
    result: dict[str, Any] = {
        "ok": False,
        "status": "fail",
        "manifest_path": str(manifest_file),
        "plugin_root": str(root),
        "schema_version": None,
        "supported_bundle_major": supported_bundle_major,
        "bundle_major": None,
        "bundle_version": "",
        "source_commit": "",
        "snapshot_id": "",
        "projection_id": "",
        "event_watermark": None,
        "checks": {},
        "resource_reports": [],
        "blockers": [],
        "warnings": [],
        "events": [],
    }
    blockers: list[str] = result["blockers"]
    warnings: list[str] = result["warnings"]
    checks: dict[str, str] = result["checks"]

    try:
        manifest = _load_manifest(manifest_file)
    except Exception as exc:  # noqa: BLE001 - surface local file read/parse failures.
        blockers.append(f"cannot read self graph bundle manifest: {exc}")
        checks["manifest"] = "fail"
        return _finalize(result)

    checks["manifest"] = "pass"
    schema_version = _int_or_none(manifest.get("schema_version"))
    bundle_major = _int_or_none(manifest.get("bundle_major"))
    event_watermark = _int_or_none(manifest.get("event_watermark"))
    result.update({
        "schema_version": schema_version,
        "bundle_major": bundle_major,
        "bundle_version": str(manifest.get("bundle_version") or ""),
        "source_commit": str(manifest.get("source_commit") or ""),
        "snapshot_id": str(manifest.get("snapshot_id") or ""),
        "projection_id": str(manifest.get("projection_id") or ""),
        "event_watermark": event_watermark,
    })

    if schema_version != SELF_GRAPH_BUNDLE_SCHEMA_VERSION:
        blockers.append(
            "unsupported self graph bundle manifest schema "
            f"{schema_version!r}; expected {SELF_GRAPH_BUNDLE_SCHEMA_VERSION}"
        )
        checks["manifest_schema"] = "fail"
    else:
        checks["manifest_schema"] = "pass"

    missing = [
        key
        for key in ("bundle_version", "source_commit", "snapshot_id", "projection_id", "event_watermark")
        if manifest.get(key) in (None, "")
    ]
    if bundle_major is None:
        missing.append("bundle_major")
    if missing:
        blockers.append("self graph bundle manifest missing required fields: " + ", ".join(sorted(missing)))
        checks["required_fields"] = "fail"
    else:
        checks["required_fields"] = "pass"

    if bundle_major is not None:
        if bundle_major > supported_bundle_major:
            checks["bundle_major"] = "fail"
            blockers.append(
                f"self graph bundle major {bundle_major} requires plugin runtime support "
                f"for major >= {bundle_major}; installed runtime supports {supported_bundle_major}"
            )
            if emit_update_event:
                result["events"].append(_update_reminder_event(manifest, bundle_major, supported_bundle_major))
        elif bundle_major < supported_bundle_major:
            checks["bundle_major"] = "warn"
            warnings.append(
                f"self graph bundle major {bundle_major} is older than runtime-supported major "
                f"{supported_bundle_major}"
            )
        else:
            checks["bundle_major"] = "pass"

    path_findings = _path_safety_findings(manifest)
    if path_findings:
        blockers.extend(path_findings)
        checks["path_safety"] = "fail"
    else:
        checks["path_safety"] = "pass"

    resource_reports, resource_blockers, resource_warnings = _check_resources(manifest, root)
    result["resource_reports"] = resource_reports
    blockers.extend(resource_blockers)
    warnings.extend(resource_warnings)
    if resource_blockers:
        checks["resources"] = "fail"
    elif resource_warnings:
        checks["resources"] = "warn"
    else:
        checks["resources"] = "pass"

    return _finalize(result)


def format_self_graph_bundle_check(result: dict[str, Any]) -> str:
    """Render a compact human-readable report for CLI/plugin diagnostics."""

    lines = [
        "Aming Claw self graph bundle",
        f"  manifest:       {result.get('manifest_path', '')}",
        f"  overall:        {result.get('status', 'unknown')}",
        f"  bundle version: {result.get('bundle_version', '') or '-'}",
        f"  bundle major:   {result.get('bundle_major', '-')}",
        f"  supported:      {result.get('supported_bundle_major', '-')}",
    ]
    blockers = result.get("blockers") or []
    warnings = result.get("warnings") or []
    events = result.get("events") or []
    if blockers:
        lines.append("")
        lines.append("Blockers:")
        lines.extend(f"  - {item}" for item in blockers)
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  - {item}" for item in warnings)
    if events:
        lines.append("")
        lines.append("Events:")
        for event in events:
            event_type = event.get("event_type", "unknown")
            reason = event.get("reason", "")
            action = event.get("recommended_action", "")
            suffix = f" - {reason}" if reason else ""
            lines.append(f"  - {event_type}{suffix}")
            if action:
                lines.append(f"    {action}")
    return "\n".join(lines)


def _resolve_plugin_root(plugin_root: str | Path | None) -> Path:
    if plugin_root:
        return Path(plugin_root).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _resolve_manifest_path(manifest_path: str | Path | None, plugin_root: Path) -> Path:
    if manifest_path:
        path = Path(manifest_path).expanduser()
        if not path.is_absolute():
            path = plugin_root / path
        return path.resolve()
    return (plugin_root / SELF_GRAPH_BUNDLE_MANIFEST_REL_PATH).resolve()


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a JSON object")
    return payload


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _path_safety_findings(manifest: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    for json_path, value in _walk_strings(manifest):
        text = value.strip()
        if not text:
            continue
        if text.startswith("~") or text.startswith("/"):
            findings.append(f"manifest contains absolute/local path at {json_path}: {text}")
        elif re.match(r"^[A-Za-z]:[\\/]", text):
            findings.append(f"manifest contains Windows absolute path at {json_path}: {text}")
        elif "/Users/" in text or "\\Users\\" in text:
            findings.append(f"manifest contains user-home path at {json_path}: {text}")
    return findings


def _walk_strings(value: Any, prefix: str = "$") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(prefix, value)]
    if isinstance(value, dict):
        pairs: list[tuple[str, str]] = []
        for key, child in value.items():
            pairs.extend(_walk_strings(child, f"{prefix}.{key}"))
        return pairs
    if isinstance(value, list):
        pairs = []
        for idx, child in enumerate(value):
            pairs.extend(_walk_strings(child, f"{prefix}[{idx}]"))
        return pairs
    return []


def _check_resources(
    manifest: dict[str, Any],
    plugin_root: Path,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    raw_resources = manifest.get("resources")
    if raw_resources is None:
        return [], [], ["self graph bundle manifest has no resources list"]
    if not isinstance(raw_resources, list):
        return [], ["self graph bundle manifest resources must be a list"], []

    reports: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    for idx, raw in enumerate(raw_resources):
        if not isinstance(raw, dict):
            blockers.append(f"resources[{idx}] must be an object")
            continue
        rel_path = str(raw.get("path") or "").replace("\\", "/").strip()
        required = bool(raw.get("required", True))
        report = {
            "path": rel_path,
            "required": required,
            "exists": False,
            "sha256": str(raw.get("sha256") or ""),
            "status": "fail",
        }
        if not rel_path:
            blockers.append(f"resources[{idx}] missing path")
            reports.append(report)
            continue
        if rel_path.startswith("/") or rel_path.startswith("../") or "/../" in rel_path:
            blockers.append(f"resources[{idx}] path must stay inside plugin root: {rel_path}")
            reports.append(report)
            continue

        path = (plugin_root / rel_path).resolve()
        try:
            path.relative_to(plugin_root)
        except ValueError:
            blockers.append(f"resources[{idx}] path escapes plugin root: {rel_path}")
            reports.append(report)
            continue

        report["resolved_path"] = str(path)
        if not path.is_file():
            message = f"required self graph bundle resource missing: {rel_path}"
            if required:
                blockers.append(message)
            else:
                warnings.append(message)
            reports.append(report)
            continue
        report["exists"] = True

        expected_hash = _normalize_sha256(raw.get("sha256"))
        if expected_hash:
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            report["actual_sha256"] = actual_hash
            if actual_hash != expected_hash:
                blockers.append(
                    f"self graph bundle resource checksum mismatch for {rel_path}: "
                    f"expected {expected_hash}, got {actual_hash}"
                )
                reports.append(report)
                continue
        report["status"] = "pass"
        reports.append(report)
    return reports, blockers, warnings


def _normalize_sha256(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:"):]
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


def _update_reminder_event(
    manifest: dict[str, Any],
    bundle_major: int,
    supported_bundle_major: int,
) -> dict[str, Any]:
    return {
        "event_type": "plugin_update_reminder",
        "event_kind": "self_graph_bundle_compatibility",
        "severity": "blocker",
        "reason": "self_graph_bundle_major_unsupported",
        "bundle_major": bundle_major,
        "supported_bundle_major": supported_bundle_major,
        "bundle_version": str(manifest.get("bundle_version") or ""),
        "source_commit": str(manifest.get("source_commit") or ""),
        "snapshot_id": str(manifest.get("snapshot_id") or ""),
        "projection_id": str(manifest.get("projection_id") or ""),
        "recommended_action": (
            "Run `aming-claw plugin update --check`, apply the compatible plugin update, "
            "then reload the agent session before importing this bundle."
        ),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def _finalize(result: dict[str, Any]) -> dict[str, Any]:
    blockers = result.get("blockers") or []
    warnings = result.get("warnings") or []
    result["ok"] = not blockers
    result["status"] = "fail" if blockers else ("warn" if warnings else "pass")
    return result
