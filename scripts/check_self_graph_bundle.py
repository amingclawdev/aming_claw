#!/usr/bin/env python3
"""Check packaged self graph bundle compatibility."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.governance.self_graph_bundle_check import (  # noqa: E402
    SUPPORTED_SELF_GRAPH_BUNDLE_MAJOR,
    check_self_graph_bundle,
    format_self_graph_bundle_check,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check the packaged Aming Claw self graph bundle.")
    parser.add_argument("--plugin-root", default="", help="Plugin checkout/root path. Defaults to this repository.")
    parser.add_argument("--manifest", default="", help="Manifest path, absolute or relative to --plugin-root.")
    parser.add_argument(
        "--supported-major",
        type=int,
        default=SUPPORTED_SELF_GRAPH_BUNDLE_MAJOR,
        help="Runtime-supported self graph bundle major version.",
    )
    parser.add_argument("--json-output", action="store_true", help="Print machine-readable JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = check_self_graph_bundle(
        plugin_root=args.plugin_root or None,
        manifest_path=args.manifest or None,
        supported_bundle_major=args.supported_major,
    )
    if args.json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(format_self_graph_bundle_check(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
