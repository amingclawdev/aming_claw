#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
HOSTS="${HOSTS:-both}"
OUT_DIR="${OUT_DIR:-}"
PLUGIN_REPO_URL="${PLUGIN_REPO_URL:-file:///plugin-source}"
PLUGIN_REF="${PLUGIN_REF:-}"
AI_PROMPT_MODE="${AI_PROMPT_MODE:-required}"
AUTH_MODE="${AUTH_MODE:-AUTH_REUSED_FROM_HOST}"
CODEX_AUTH_HOME="${CODEX_AUTH_HOME:-}"
CLAUDE_AUTH_HOME="${CLAUDE_AUTH_HOME:-}"
DOCKER_AI_E2E_CHANGED_FILES="${DOCKER_AI_E2E_CHANGED_FILES:-}"
DOCKER_LIVE_OBSERVER_ROUTE="${DOCKER_LIVE_OBSERVER_ROUTE:-0}"
LIVE_OBSERVER_ROUTE_REPORT_PATH="${LIVE_OBSERVER_ROUTE_REPORT_PATH:-}"

usage() {
  cat <<'USAGE'
Usage: docker/hn-install-audit/run-install-audit.sh [options]

Options:
  --host codex|claude|both      Lane(s) to run. Default: both.
  --run-id ID                   Stable run id. Default: UTC timestamp.
  --out DIR                     Output directory. Default: docs/hn-demo/audits/install-$RUN_ID.
  --repo-url URL                Repo URL visible inside container. Default: file:///plugin-source.
  --ref REF                     Optional git ref after clone.
  --ai-prompt-mode MODE         required|optional|skip. Default: required.
  --codex-auth-home DIR         Read Codex auth from DIR instead of $HOME.
  --claude-auth-home DIR        Read Claude auth from DIR instead of $HOME.
  --changed-files LIST          Newline or comma separated changed files for lane impact planning.
  --no-build                    Reuse existing Docker images.
  --help                        Show this help.

Mode B auth reuse:
  Codex lane mounts <auth-home>/.codex read-only when it exists.
  Claude lane mounts <auth-home>/.claude and <auth-home>/.claude.json read-only
  when they exist.
  Token files are never baked into images; reports label AUTH_REUSED_FROM_HOST.
USAGE
}

NO_BUILD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOSTS="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    --repo-url) PLUGIN_REPO_URL="$2"; shift 2 ;;
    --ref) PLUGIN_REF="$2"; shift 2 ;;
    --ai-prompt-mode) AI_PROMPT_MODE="$2"; shift 2 ;;
    --codex-auth-home) CODEX_AUTH_HOME="$2"; shift 2 ;;
    --claude-auth-home) CLAUDE_AUTH_HOME="$2"; shift 2 ;;
    --changed-files) DOCKER_AI_E2E_CHANGED_FILES="$2"; shift 2 ;;
    --no-build) NO_BUILD=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

case "$HOSTS" in
  codex|claude|both) ;;
  *) echo "--host must be codex, claude, or both" >&2; exit 2 ;;
esac

if [[ -z "$OUT_DIR" ]]; then
  OUT_DIR="$ROOT/docs/hn-demo/audits/install-$RUN_ID"
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not reachable. Start Docker Desktop or dockerd before running install audit." >&2
  exit 2
fi

mkdir -p "$OUT_DIR"

build_image() {
  local host="$1"
  local image="aming-claw-install-audit-$host"
  if [[ "$NO_BUILD" == "0" ]]; then
    docker build -t "$image" -f "$ROOT/docker/hn-install-audit/$host/Dockerfile" "$ROOT"
  fi
}

auth_mounts() {
  local host="$1"
  local mounts=()
  if [[ "$host" == "codex" ]]; then
    local auth_home="${CODEX_AUTH_HOME:-$HOME}"
    if [[ -d "$auth_home/.codex" ]]; then
      mounts+=("-v" "$auth_home/.codex:/host-auth/codex:ro")
    elif [[ -d "$auth_home" && -f "$auth_home/auth.json" ]]; then
      mounts+=("-v" "$auth_home:/host-auth/codex:ro")
    fi
  else
    local auth_home="${CLAUDE_AUTH_HOME:-$HOME}"
    if [[ -d "$auth_home/.claude" ]]; then
      mounts+=("-v" "$auth_home/.claude:/host-auth/claude:ro")
    elif [[ -d "$auth_home" && ( -f "$auth_home/.credentials.json" || -f "$auth_home/credentials.json" || -f "$auth_home/auth.json" ) ]]; then
      mounts+=("-v" "$auth_home:/host-auth/claude:ro")
    fi
    if [[ -f "$auth_home/.claude.json" ]]; then
      mounts+=("-v" "$auth_home/.claude.json:/host-auth/claude-home.json:ro")
    elif [[ -f "$HOME/.claude.json" && -z "$CLAUDE_AUTH_HOME" ]]; then
      mounts+=("-v" "$HOME/.claude.json:/host-auth/claude-home.json:ro")
    fi
  fi
  printf '%s\n' "${mounts[@]}"
}

run_host() {
  local host="$1"
  local image="aming-claw-install-audit-$host"
  build_image "$host" || return $?

  local mounts=()
  while IFS= read -r item; do
    [[ -n "$item" ]] && mounts+=("$item")
  done < <(auth_mounts "$host")

  docker run --rm \
    "${mounts[@]}" \
    -v "$ROOT:/plugin-source:ro" \
    -v "$OUT_DIR:/audit-output" \
    -v "$ROOT/docker/hn-install-audit/common/install-audit.mjs:/opt/hn-install-audit/install-audit.mjs:ro" \
    -v "$ROOT/docker/hn-install-audit/common/state-manager.mjs:/opt/hn-install-audit/state-manager.mjs:ro" \
    -v "$ROOT/docker/hn-install-audit/validate-report.mjs:/opt/hn-install-audit/validate-report.mjs:ro" \
    -e "AI_HOST=$host" \
    -e "RUN_ID=$RUN_ID" \
    -e "PLUGIN_REPO_URL=$PLUGIN_REPO_URL" \
    -e "PLUGIN_REF=$PLUGIN_REF" \
    -e "AI_PROMPT_MODE=$AI_PROMPT_MODE" \
    -e "AUTH_MODE=$AUTH_MODE" \
    -e "DOCKER_AI_E2E_CHANGED_FILES=$DOCKER_AI_E2E_CHANGED_FILES" \
    -e "DOCKER_LIVE_OBSERVER_ROUTE=$DOCKER_LIVE_OBSERVER_ROUTE" \
    -e "LIVE_OBSERVER_ROUTE_REPORT_PATH=$LIVE_OBSERVER_ROUTE_REPORT_PATH" \
    "$image"
}

failures=0

if [[ "$HOSTS" == "codex" || "$HOSTS" == "both" ]]; then
  if ! run_host codex; then
    echo "codex install audit lane failed" >&2
    failures=$((failures + 1))
  fi
fi
if [[ "$HOSTS" == "claude" || "$HOSTS" == "both" ]]; then
  if ! run_host claude; then
    echo "claude install audit lane failed" >&2
    failures=$((failures + 1))
  fi
fi

echo "Install audit artifacts: $OUT_DIR"
exit "$failures"
