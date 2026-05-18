"""Backlog insert triage gate. Actions: admit, reject_dup, supersede, merge_into."""
from __future__ import annotations
import json, logging, re
log = logging.getLogger(__name__)

_GENERIC_TITLE_TOKENS = {
    "agent",
    "api",
    "audit",
    "backlog",
    "bug",
    "code",
    "codex",
    "file",
    "files",
    "graph",
    "governance",
    "hardening",
    "issue",
    "issues",
    "need",
    "needs",
    "opt",
    "performance",
    "query",
    "queries",
    "row",
    "rows",
    "server",
    "test",
    "tests",
}

def _parse_tf(v):
    if isinstance(v, str):
        try: return json.loads(v)
        except Exception: return []
    return v or []

def _title_tokens(title: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", str(title or "").lower())
        if len(token) >= 3
    }
    return {token for token in tokens if token not in _GENERIC_TITLE_TOKENS}

def _decision(action: str, reason: str, related_bug_ids: list[str], confidence: float, **evidence) -> dict:
    result = {
        "action": action,
        "reason": reason,
        "related_bug_ids": related_bug_ids,
        "confidence": confidence,
    }
    if evidence:
        result["evidence"] = evidence
    return result

def _candidate(row: dict, **evidence) -> dict:
    result = {
        "bug_id": row.get("bug_id", ""),
        "title": row.get("title", ""),
        "target_files": _parse_tf(row.get("target_files", [])),
    }
    if evidence:
        result["evidence"] = evidence
    return result

def triage_backlog_insert(payload: dict, open_rows: list[dict]) -> dict:
    """Classify a new backlog filing against existing OPEN rows.
    Returns dict with keys: action, reason, related_bug_ids, confidence."""
    title = payload.get("title", "")
    tf = _parse_tf(payload.get("target_files", []))
    if not open_rows:
        return {"action": "admit", "reason": "no open rows", "related_bug_ids": [], "confidence": 1.0}
    title_tokens = _title_tokens(title)
    for row in open_rows:
        rid, rt, rtf = row.get("bug_id", ""), row.get("title", ""), _parse_tf(row.get("target_files", []))
        if title and rt and title.strip().lower() == rt.strip().lower():
            return _decision(
                "reject_dup",
                "duplicate of %s" % rid,
                [rid],
                0.9,
                candidates=[_candidate(row, title_exact_match=True)],
            )
        if tf and rtf:
            ov = set(tf) & set(rtf)
            if len(ov) >= len(tf) and len(ov) >= len(rtf):
                evidence = {
                    "overlap_files": sorted(ov),
                    "overlap_count": len(ov),
                }
                return _decision(
                    "supersede",
                    "supersedes %s" % rid,
                    [rid],
                    0.8,
                    **evidence,
                    candidates=[_candidate(row, **evidence)],
                )
            row_title_tokens = _title_tokens(rt)
            title_overlap = sorted(title_tokens & row_title_tokens)
            if ov and (len(ov) >= 2 or title_overlap):
                evidence = {
                    "overlap_files": sorted(ov),
                    "overlap_count": len(ov),
                    "title_token_overlap": title_overlap,
                }
                return _decision(
                    "merge_into",
                    "merge into %s" % rid,
                    [rid],
                    0.7,
                    **evidence,
                    candidates=[_candidate(row, **evidence)],
                )
    return {"action": "admit", "reason": "no significant overlap", "related_bug_ids": [], "confidence": 0.8}
