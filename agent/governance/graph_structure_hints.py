"""Scan source-controlled graph structure hint blocks.

Hints are source truth for manual graph structure corrections. This module only
indexes blocks; projection/materialization lives in graph_hint_projection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


_START_RE = re.compile(r"aming-claw-hint:start\s+(?P<attrs>.*)$")
_END_RE = re.compile(r"aming-claw-hint:end")
_ATTR_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*)=(?P<value>\"[^\"]*\"|'[^']*'|\S+)")
_DEF_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b")


@dataclass(frozen=True)
class GraphStructureHint:
    hint_id: str
    op: str
    source_path: str
    target_node_id: str = ""
    edge: str = ""
    role: str = ""
    reason: str = ""
    evidence: str = ""
    line_start: int = 0
    line_end: int = 0
    anchor_symbol: str = ""
    status: str = "indexed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "hint_id": self.hint_id,
            "op": self.op,
            "edge": self.edge,
            "role": self.role,
            "target_node_id": self.target_node_id,
            "source_path": self.source_path,
            "reason": self.reason,
            "evidence": self.evidence,
            "anchor": {
                "symbol": self.anchor_symbol,
                "line_start": self.line_start,
                "line_end": self.line_end,
            },
            "status": self.status,
        }


def scan_graph_structure_hints(files: Mapping[str, str]) -> dict[str, Any]:
    """Return a deterministic index of graph structure hints from text files."""
    hints: list[GraphStructureHint] = []
    for source_path in sorted(files):
        hints.extend(_scan_one_file(source_path, files[source_path] or ""))
    return {
        "hint_count": len(hints),
        "hints": [hint.to_dict() for hint in hints],
    }


def _scan_one_file(source_path: str, text: str) -> list[GraphStructureHint]:
    hints: list[GraphStructureHint] = []
    lines = text.splitlines()
    current_symbol = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        symbol_match = _DEF_RE.match(line)
        if symbol_match:
            current_symbol = symbol_match.group(1)
        start_match = _START_RE.search(line)
        if not start_match:
            index += 1
            continue

        attrs = _parse_attrs(start_match.group("attrs"))
        body: list[str] = []
        line_start = index + 1
        line_end = line_start
        index += 1
        while index < len(lines):
            line_end = index + 1
            if _END_RE.search(lines[index]):
                break
            body.append(lines[index])
            index += 1
        hints.append(
            GraphStructureHint(
                hint_id=str(attrs.get("id") or ""),
                op=str(attrs.get("op") or ""),
                edge=str(attrs.get("edge") or ""),
                role=str(attrs.get("role") or ""),
                target_node_id=str(attrs.get("target") or attrs.get("target_node_id") or ""),
                source_path=source_path,
                reason=_body_value(body, "reason"),
                evidence=_body_value(body, "evidence"),
                line_start=line_start,
                line_end=line_end,
                anchor_symbol=current_symbol,
            )
        )
        index += 1
    return hints


def _parse_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw or ""):
        value = match.group("value").strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        attrs[match.group("key")] = value
    return attrs


def _body_value(lines: list[str], key: str) -> str:
    prefix = f"{key}:"
    for raw in lines:
        text = raw.strip()
        for marker in ("#", "//", "<!--"):
            if text.startswith(marker):
                text = text[len(marker):].strip()
                break
        if text.startswith(prefix):
            return text[len(prefix):].strip().strip("-").strip()
    return ""
