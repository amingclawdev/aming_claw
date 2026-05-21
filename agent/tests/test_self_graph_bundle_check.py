from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from agent.governance.self_graph_bundle_check import check_self_graph_bundle


ROOT = Path(__file__).resolve().parents[2]


def _write_bundle_fixture(tmp_path: Path, *, bundle_major: int = 1, sha256: str | None = None) -> Path:
    resource_dir = tmp_path / "agent" / "mcp" / "resources"
    resource_dir.mkdir(parents=True)
    seed = resource_dir / "seed-graph-summary.json"
    seed.write_text(json.dumps({"schema_version": 1, "project_id": "aming-claw"}), encoding="utf-8")
    expected_hash = sha256 if sha256 is not None else hashlib.sha256(seed.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "bundle_kind": "aming_claw_self_graph_semantic_bundle",
        "bundle_major": bundle_major,
        "bundle_version": f"{bundle_major}.0.0",
        "project_id": "aming-claw",
        "source_commit": "abc1234",
        "snapshot_id": "scope-abc1234-test",
        "projection_id": "semproj-abc1234-test",
        "event_watermark": 7,
        "resources": [
            {
                "path": "agent/mcp/resources/seed-graph-summary.json",
                "role": "seed_graph_summary",
                "required": True,
                "sha256": expected_hash,
            }
        ],
    }
    manifest_path = resource_dir / "self-graph-bundle-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_self_graph_bundle_major_one_passes_with_matching_resource_hash(tmp_path: Path):
    _write_bundle_fixture(tmp_path, bundle_major=1)

    result = check_self_graph_bundle(plugin_root=tmp_path, supported_bundle_major=1)

    assert result["ok"] is True
    assert result["status"] == "pass"
    assert result["bundle_major"] == 1
    assert result["checks"]["bundle_major"] == "pass"
    assert result["checks"]["resources"] == "pass"
    assert result["events"] == []


def test_newer_self_graph_bundle_major_emits_update_reminder_event(tmp_path: Path):
    _write_bundle_fixture(tmp_path, bundle_major=2)

    result = check_self_graph_bundle(plugin_root=tmp_path, supported_bundle_major=1)

    assert result["ok"] is False
    assert result["status"] == "fail"
    assert result["checks"]["bundle_major"] == "fail"
    assert "major 2" in result["blockers"][0]
    assert result["events"][0]["event_type"] == "plugin_update_reminder"
    assert result["events"][0]["reason"] == "self_graph_bundle_major_unsupported"
    assert result["events"][0]["bundle_major"] == 2
    assert result["events"][0]["supported_bundle_major"] == 1


def test_self_graph_bundle_check_rejects_resource_checksum_mismatch(tmp_path: Path):
    _write_bundle_fixture(tmp_path, bundle_major=1, sha256="0" * 64)

    result = check_self_graph_bundle(plugin_root=tmp_path, supported_bundle_major=1)

    assert result["ok"] is False
    assert result["checks"]["resources"] == "fail"
    assert "checksum mismatch" in result["blockers"][0]


def test_check_self_graph_bundle_script_outputs_update_event_json(tmp_path: Path):
    _write_bundle_fixture(tmp_path, bundle_major=2)

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_self_graph_bundle.py"),
            "--plugin-root",
            str(tmp_path),
            "--supported-major",
            "1",
            "--json-output",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["events"][0]["event_type"] == "plugin_update_reminder"
