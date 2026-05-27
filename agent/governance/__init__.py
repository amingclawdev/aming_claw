"""Workflow Governance Service

Three-layer architecture:
  Layer 1: Graph Definition (rule layer, JSON + NetworkX)
  Layer 2: Runtime State   (runtime state, SQLite)
  Layer 3: Event Log       (event stream, JSONL append-only)
"""

__version__ = "0.1.1"
