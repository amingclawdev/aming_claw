"""Unified commit-bound graph asset projection tables.

Source files, reviewed hints, and reconcile output remain the source of truth.
These tables store the replayable projection used by runtime queries, gates,
and dashboard surfaces for doc/test/config assets.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "graph_asset_projection.v1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_asset_projection (
    project_id              TEXT NOT NULL,
    snapshot_id             TEXT NOT NULL DEFAULT '',
    run_id                  TEXT NOT NULL DEFAULT '',
    commit_sha              TEXT NOT NULL DEFAULT '',
    asset_kind              TEXT NOT NULL DEFAULT '',
    asset_path              TEXT NOT NULL DEFAULT '',
    file_kind               TEXT NOT NULL DEFAULT '',
    sha256                  TEXT NOT NULL DEFAULT '',
    file_hash               TEXT NOT NULL DEFAULT '',
    size_bytes              INTEGER NOT NULL DEFAULT 0,
    scan_status             TEXT NOT NULL DEFAULT '',
    graph_status            TEXT NOT NULL DEFAULT '',
    binding_status          TEXT NOT NULL DEFAULT '',
    impact_scope_policy     TEXT NOT NULL DEFAULT '',
    accepted_bindings_json  TEXT NOT NULL DEFAULT '[]',
    binding_candidates_json TEXT NOT NULL DEFAULT '[]',
    metadata_json           TEXT NOT NULL DEFAULT '{}',
    source_projection       TEXT NOT NULL DEFAULT '',
    updated_at              TEXT NOT NULL,
    PRIMARY KEY (project_id, snapshot_id, commit_sha, asset_kind, asset_path)
);
CREATE INDEX IF NOT EXISTS idx_graph_asset_projection_snapshot
    ON graph_asset_projection (project_id, snapshot_id, asset_kind, binding_status);
CREATE INDEX IF NOT EXISTS idx_graph_asset_projection_path
    ON graph_asset_projection (project_id, asset_kind, asset_path);

CREATE TABLE IF NOT EXISTS graph_asset_bindings (
    project_id          TEXT NOT NULL,
    snapshot_id         TEXT NOT NULL DEFAULT '',
    commit_sha          TEXT NOT NULL DEFAULT '',
    asset_kind          TEXT NOT NULL DEFAULT '',
    asset_path          TEXT NOT NULL DEFAULT '',
    binding_status      TEXT NOT NULL DEFAULT '',
    node_id             TEXT NOT NULL DEFAULT '',
    title               TEXT NOT NULL DEFAULT '',
    role                TEXT NOT NULL DEFAULT '',
    source              TEXT NOT NULL DEFAULT '',
    binding_key         TEXT NOT NULL DEFAULT '',
    evidence_json       TEXT NOT NULL DEFAULT '{}',
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (project_id, snapshot_id, commit_sha, asset_kind, asset_path, binding_status, node_id, binding_key)
);
CREATE INDEX IF NOT EXISTS idx_graph_asset_bindings_node
    ON graph_asset_bindings (project_id, snapshot_id, node_id, asset_kind, binding_status);
CREATE INDEX IF NOT EXISTS idx_graph_asset_bindings_path
    ON graph_asset_bindings (project_id, asset_kind, asset_path);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _text(value: Any) -> str:
    return str(value or "")


def _norm_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip("/")


def _json(value: Any, default: Any) -> str:
    if value is None:
        value = default
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return json.dumps({"unserializable": repr(value)}, ensure_ascii=False, sort_keys=True)


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def _binding_key(binding: Mapping[str, Any], *, fallback: str) -> str:
    explicit = str(binding.get("proposal_hash") or binding.get("binding_key") or "").strip()
    if explicit:
        return explicit
    payload = json.dumps(binding, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256((fallback + "\n" + payload).encode("utf-8")).hexdigest()


def _projection_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["accepted_bindings"] = _loads(out.pop("accepted_bindings_json", "[]"), [])
    out["binding_candidates"] = _loads(out.pop("binding_candidates_json", "[]"), [])
    out["metadata"] = _loads(out.pop("metadata_json", "{}"), {})
    return out


def _binding_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["evidence"] = _loads(out.pop("evidence_json", "{}"), {})
    return out


def upsert_asset_projection_rows(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    snapshot_id: str,
    commit_sha: str,
    asset_kind: str,
    rows: Iterable[Mapping[str, Any]],
    run_id: str = "",
    source_projection: str = SCHEMA_VERSION,
    replace_existing: bool = True,
) -> dict[str, Any]:
    """Replace the projection rows for one asset kind at one snapshot/commit."""

    ensure_schema(conn)
    pid = _text(project_id)
    sid = _text(snapshot_id)
    commit = _text(commit_sha)
    kind = _text(asset_kind)
    rid = _text(run_id)
    updated_at = _utc_iso()
    normalized = [dict(row) for row in rows if isinstance(row, Mapping)]

    if replace_existing:
        conn.execute(
            """DELETE FROM graph_asset_bindings
               WHERE project_id = ? AND snapshot_id = ? AND commit_sha = ? AND asset_kind = ?""",
            (pid, sid, commit, kind),
        )
        conn.execute(
            """DELETE FROM graph_asset_projection
               WHERE project_id = ? AND snapshot_id = ? AND commit_sha = ? AND asset_kind = ?""",
            (pid, sid, commit, kind),
        )

    projection_count = 0
    binding_count = 0
    for row in normalized:
        path = _norm_path(row.get("path") or row.get("asset_path"))
        if not path:
            continue
        accepted = row.get("accepted_bindings") if isinstance(row.get("accepted_bindings"), list) else []
        candidates = row.get("binding_candidates") if isinstance(row.get("binding_candidates"), list) else []
        metadata = {
            "schema_version": row.get("schema_version") or "",
            "doc_kind": row.get("doc_kind") or "",
        }
        conn.execute(
            """INSERT OR REPLACE INTO graph_asset_projection
               (project_id, snapshot_id, run_id, commit_sha, asset_kind, asset_path,
                file_kind, sha256, file_hash, size_bytes, scan_status, graph_status,
                binding_status, impact_scope_policy, accepted_bindings_json,
                binding_candidates_json, metadata_json, source_projection, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pid,
                sid,
                _text(row.get("run_id") or rid),
                _text(row.get("commit_sha") or commit),
                kind,
                path,
                _text(row.get("file_kind") or row.get("doc_kind")),
                _text(row.get("sha256")),
                _text(row.get("file_hash")),
                int(row.get("size_bytes") or 0),
                _text(row.get("scan_status")),
                _text(row.get("graph_status")),
                _text(row.get("binding_status")),
                _text(row.get("impact_scope_policy")),
                _json(accepted, []),
                _json(candidates, []),
                _json(metadata, {}),
                _text(source_projection),
                updated_at,
            ),
        )
        projection_count += 1
        for binding_status, bindings in (("accepted", accepted), ("candidate", candidates)):
            for binding in bindings:
                if not isinstance(binding, Mapping):
                    continue
                node_id = _text(
                    binding.get("node_id")
                    or binding.get("target_node_id")
                    or binding.get("target_id")
                )
                if not node_id:
                    continue
                fallback = f"{pid}:{sid}:{commit}:{kind}:{path}:{binding_status}:{node_id}"
                binding_key = _binding_key(binding, fallback=fallback)
                conn.execute(
                    """INSERT OR REPLACE INTO graph_asset_bindings
                       (project_id, snapshot_id, commit_sha, asset_kind, asset_path,
                        binding_status, node_id, title, role, source, binding_key,
                        evidence_json, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        pid,
                        sid,
                        commit,
                        kind,
                        path,
                        binding_status,
                        node_id,
                        _text(binding.get("title") or binding.get("target_title")),
                        _text(binding.get("role") or kind),
                        _text(binding.get("source")),
                        binding_key,
                        _json(dict(binding), {}),
                        updated_at,
                    ),
                )
                binding_count += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": pid,
        "snapshot_id": sid,
        "commit_sha": commit,
        "asset_kind": kind,
        "projection_count": projection_count,
        "binding_count": binding_count,
        "updated_at": updated_at,
    }


def upsert_doc_asset_projection(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    snapshot_id: str,
    doc_asset_state: Mapping[str, Any],
    source_projection: str = "doc_asset_state",
) -> dict[str, Any]:
    docs = doc_asset_state.get("docs") if isinstance(doc_asset_state, Mapping) else []
    return upsert_asset_projection_rows(
        conn,
        project_id=project_id,
        snapshot_id=snapshot_id,
        commit_sha=_text(doc_asset_state.get("commit_sha") if isinstance(doc_asset_state, Mapping) else ""),
        asset_kind="doc",
        rows=docs if isinstance(docs, list) else [],
        run_id=_text(doc_asset_state.get("run_id") if isinstance(doc_asset_state, Mapping) else ""),
        source_projection=source_projection,
        replace_existing=True,
    )


def list_asset_projection(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    snapshot_id: str = "",
    commit_sha: str = "",
    asset_kind: str = "",
    asset_path: str = "",
    binding_status: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    clauses = ["project_id = ?"]
    params: list[Any] = [_text(project_id)]
    for column, value in (
        ("snapshot_id", snapshot_id),
        ("commit_sha", commit_sha),
        ("asset_kind", asset_kind),
        ("asset_path", _norm_path(asset_path) if asset_path else ""),
        ("binding_status", binding_status),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    params.append(max(1, min(int(limit or 500), 5000)))
    rows = conn.execute(
        f"""SELECT * FROM graph_asset_projection
            WHERE {' AND '.join(clauses)}
            ORDER BY asset_kind, asset_path
            LIMIT ?""",
        params,
    ).fetchall()
    return [_projection_row(row) for row in rows]


def list_asset_bindings_for_node(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    snapshot_id: str,
    node_id: str,
    asset_kind: str = "",
    binding_status: str = "accepted",
    limit: int = 500,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    clauses = ["project_id = ?", "snapshot_id = ?", "node_id = ?"]
    params: list[Any] = [_text(project_id), _text(snapshot_id), _text(node_id)]
    if asset_kind:
        clauses.append("asset_kind = ?")
        params.append(_text(asset_kind))
    if binding_status:
        clauses.append("binding_status = ?")
        params.append(_text(binding_status))
    params.append(max(1, min(int(limit or 500), 5000)))
    rows = conn.execute(
        f"""SELECT * FROM graph_asset_bindings
            WHERE {' AND '.join(clauses)}
            ORDER BY asset_kind, asset_path, binding_status
            LIMIT ?""",
        params,
    ).fetchall()
    return [_binding_row(row) for row in rows]


__all__ = [
    "SCHEMA_VERSION",
    "ensure_schema",
    "list_asset_bindings_for_node",
    "list_asset_projection",
    "upsert_asset_projection_rows",
    "upsert_doc_asset_projection",
]
