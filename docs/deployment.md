# Aming Claw — Deployment Guide

> **Canonical deployment document** — Host-based deployment for local V1 usage.
> Last updated: 2026-05-21 | V1 dashboard/graph control-plane alignment

## V1 Local Plugin Path

For V1 users, the recommended deployment is local plugin mode:

1. Install from `https://github.com/amingclawdev/aming-claw`.
2. Start governance with `aming-claw start` in a separate terminal.
3. Open `http://localhost:40000/dashboard`.
4. Load the Codex or Claude Code skill/MCP in a new session.
5. Bootstrap a target project from the dashboard and build/update its graph.

This path requires governance and packaged dashboard static assets. It does not
require ServiceManager, executor, Redis, Telegram, or dbservice for the main
dashboard/graph/backlog workflow. ServiceManager/executor degraded should be
reported as a chain automation warning, not as a failed dashboard deployment.

## 1. Prerequisites

### System Requirements

- **Python 3.9+** with pip for the V1 CLI/plugin runtime
- **Node.js/npm** only when rebuilding dashboard assets from source
- **Git** (with worktree support)
- **Codex CLI** (`codex`) for OpenAI-backed local AI routes when used
- **Claude Code CLI** (`claude`) for Anthropic-backed local AI routes when used
- **Docker**, **Redis**, Telegram, and dbservice only for optional/advanced
  services

### Environment Variables

```bash
# Optional
export MEMORY_BACKEND="local"          # local | docker | cloud
export TELEGRAM_BOT_TOKEN="<token>"    # Required for Telegram gateway
export REDIS_URL="redis://localhost:40079"
```

## 2. Service Architecture

```
Host Machine (primary runtime)
├── Governance Service     :40000   ← Dashboard, graph, backlog, review, reconcile API
├── MCP Server                      ← Tool bridge for Codex/Claude sessions
├── Manager HTTP Server    :40101   ← Optional ServiceManager sidecar
├── Service Manager                 ← Optional executor supervisor
└── Executor Worker                 ← Experimental chain task execution

Optional Docker Dependencies
├── Telegram Gateway       :40010   ← Message gateway
├── dbservice              :40002   ← Semantic memory (mem0)
└── Redis                  :40079   ← Pub/sub, cache
```

**All V1 governance and dashboard operations run on the host at
`http://localhost:40000`.**

The minimum V1 path is Governance + dashboard assets + MCP visibility. The
ServiceManager, executor, Telegram gateway, Redis, and dbservice are advanced
or optional surfaces.

## 3. MCP Configuration (`.mcp.json`)

The MCP server exposes the governance tools to Claude Code. It is configured with `--workers 0`, which means **the MCP server does NOT start the executor worker or the governance service**. Both must be started separately (see §4 and §5).

```json
{
  "mcpServers": {
    "aming-claw": {
      "command": "python",
      "args": ["-m", "agent.mcp", "--workers", "0"],
      "env": {
        "GOV_PROJECT_ID": "aming-claw",
        "MEMORY_BACKEND": "local"
      }
    }
  }
}
```

Place this file in the project root. Claude Code reads it automatically on session start, but it only wires up the MCP tools — process supervision is separate.

On Windows, replace `"command": "python"` with `"command": "py"` and `"args": ["-3", "-m", "agent.mcp", "--workers", "0"]` if the bare `python` is not on `PATH`. The bootstrap script in [install/codex-bootstrap.md](install/codex-bootstrap.md) auto-detects the right Python 3.9+ interpreter.

## 4. Governance Service Startup

The governance service is a prerequisite for everything else (MCP, executor, gateway). Start it first.

### Option A: CLI start (Recommended for V1)

```bash
aming-claw start
```

This command first checks whether governance is already healthy on port
`40000`. If healthy, it prints the dashboard URL and exits. Otherwise it starts
a foreground, long-running governance service that should stay open in a
separate terminal/window.

### Option B: Legacy one-click launcher (Windows)

```powershell
# Starts governance + ServiceManager + Docker services in the correct order
.\start.ps1
```

### Option C: Direct start (advanced / dev only)

Use this only when you are debugging the governance server itself. New users
should use Option A.

```bash
# Start governance service directly on the host (not Docker)
python -m agent.governance.server --port 40000
```

### Health Check

```bash
curl http://localhost:40000/api/health
# Expected: {"status": "ok", "version": "...", "pid": ...}
```

## 5. Executor Lifecycle (Advanced / Experimental In V1)

The V1 dashboard/graph/backlog path does not require the executor. If you are
testing chain automation, the executor worker **must be launched under
ServiceManager supervision**. The MCP server will NOT start it because
`.mcp.json` uses `--workers 0`. Orphan executors started by any other means
have no crash recovery and no deploy-signal handling.

MCP may expose host-ops tools (`manager_health`, `manager_start`, `governance_redeploy`, `executor_respawn`, `runtime_status`) to call ServiceManager or its sidecar. These tools are a fixed-command repair facade, not MCP ownership of the executor lifecycle. The MCP `manager_start` bootstrap currently targets the Windows PowerShell script; an equivalent POSIX bootstrap is not yet provided — on macOS/Linux, invoke `python -m agent.service_manager` directly (see the cross-platform example below).

### Starting ServiceManager (chain/executor only)

```powershell
# Windows — acquires the singleton lock on port 39103 and supervises executor_worker
.\scripts\start-manager.ps1 -Takeover
```

```bash
# Cross-platform direct invocation; POSIX bootstrap scripts are tracked separately
python -m agent.service_manager --project aming-claw --governance-url http://localhost:40000 --workspace "$PWD"
```

Verify with `tasklist /v /fi "imagename eq python.exe" | findstr service_manager` (Windows) or `pgrep -fa service_manager.py` (Unix). The executor is only properly supervised if its parent process is `agent/service_manager.py`.

### Supervision behavior

1. **Singleton lock** — `start-manager.ps1` uses a named Windows mutex (`Global\aming_claw_manager`); a second launcher run without `-Takeover` exits. `agent/service_manager.py` itself does not bind a port, so verify supervision by process tree (see check above), not by checking any listener
2. **Monitor** — ServiceManager checks executor health every 10s
3. **Auto-restart** — If executor crashes, ServiceManager restarts it
4. **Circuit breaker** — 5 restarts within 300s triggers OPEN state (stops restart attempts)
5. **Crash recovery** — On startup, executor requeues orphaned claimed tasks
6. **Deploy signal** — ServiceManager consumes `manager_signal.json` written by the Merge stage (and by the redeploy handler for executor targets), which is how auto-chain restarts the executor after a deploy. The manager_http_server on port 40101 handles governance redeploy requests from `deploy_chain.py`

### Manual Executor Control

```bash
# Check runtime status via MCP tools
# runtime_status aggregates governance, ServiceManager, and version_check.
# executor_status returns external/no-manager for normal --workers 0 MCP sessions.

# Scale MCP-local workers only when MCP was explicitly started with --workers N
# executor_scale(0) — pause claiming (for observer mode)
# executor_scale(1) — resume claiming
```

### Session Exit

When the Claude Code session ends:
- MCP server shuts down (Claude Code child process exits)
- **ServiceManager and executor continue running** — they are independent host processes, not children of the MCP server
- Governance service also continues running

To stop the full stack, either use `start.ps1` teardown (if it exposes one) or kill ServiceManager (which stops the supervised executor) and the governance process explicitly.

## 6. Telegram Gateway

### Start via Docker Compose

```bash
docker compose -f docker-compose.governance.yml up -d telegram-gateway
```

### Configuration

The gateway connects to the host governance service:
- Gateway listens on `:40010`
- Governance URL: `http://host.docker.internal:40000` (Docker-to-host)
- Requires `TELEGRAM_BOT_TOKEN` environment variable

### Message Flow

```
Telegram → Gateway (:40010) → Governance (:40000) → Executor → Reply via Redis pub/sub
```

This is an optional remote task flow. It is not the primary V1 dashboard/graph
review path.

## 7. Redis Setup

### Via Docker Compose (Recommended)

```bash
docker compose -f docker-compose.governance.yml up -d redis
```

Redis runs on port 40079 and provides:
- Pub/sub for real-time event delivery
- Hot context store (24h TTL)
- Cache for governance data

### Connection Test

```bash
redis-cli -p 40079 ping
# Expected: PONG
```

## 8. Docker Compose for Optional Services

```bash
# Start all optional services
docker compose -f docker-compose.governance.yml up -d

# Start specific services
docker compose -f docker-compose.governance.yml up -d redis telegram-gateway dbservice

# Check service health
docker compose -f docker-compose.governance.yml ps

# View logs
docker compose -f docker-compose.governance.yml logs -f telegram-gateway
```

### Service Dependencies

| Service | Port | Depends On | Required? |
|---------|------|------------|-----------|
| Governance | 40000 | - | **Yes** for V1 dashboard/graph |
| Dashboard static assets | served by 40000 | Governance | **Yes** for dashboard UI |
| Manager HTTP | 40101 | Governance | Advanced; required for chain/executor operations |
| Executor | - | Governance + ServiceManager | Experimental in V1 |
| Redis | 40079 | - | Optional |
| Telegram GW | 40010 | Governance, Redis | Optional |
| dbservice | 40002 | — | Optional (for semantic search) |

## 9. First-Time Setup

### V1 Plugin Setup

```bash
# 1. Install from Git
git clone https://github.com/amingclawdev/aming-claw.git
cd aming-claw
pip install -e .

# 2. Start governance
aming-claw start

# 3. Open dashboard
aming-claw open
```

Then use the Projects page to bootstrap a clean target workspace and build its
graph. No governance token, Redis service, workflow graph import, or executor is
required for the V1 dashboard/graph/backlog path.

### Advanced Chain Setup

Only use this path when testing the experimental executor/chain stack:

```bash
docker compose -f docker-compose.governance.yml up -d redis
python -m agent.service_manager --project aming-claw --governance-url http://localhost:40000 --workspace "$PWD"
```

## 10. Workspace and Worktree Routing

> Experimental in V1 — worktrees are used by the chain-automation executor. Manual Fix flows commit on the main worktree and do not require worktree isolation.

The executor uses git worktrees for task isolation:

- **Main workspace** — coordinator and PM tasks execute here
- **Dev worktrees** — dev tasks get isolated `dev/task-{id}` worktrees
- **Merge** — merge tasks cherry-pick dev worktree commits to main

### Worktree Lifecycle

```
dev task created → worktree created at .worktrees/dev-task-{id}
dev task completes → worktree preserved for merge
merge task → cherry-pick to main → worktree cleaned up
```

## 11. Restart and Recovery

### After Host Reboot

```bash
aming-claw start
aming-claw open
```

For chain/executor testing, start ServiceManager separately after governance is
healthy.

### After Executor Crash

The executor automatically recovers on restart:
- Orphaned claimed tasks are requeued
- Circuit breaker resets after cooldown period
- Version gate re-syncs git HEAD to DB

### DB Lock Recovery

If governance DB becomes locked (known issue after version-update):
```bash
# Restart governance service
# This clears WAL locks and restores normal operation
```

## 12. Monitoring

### Health Endpoints

```bash
# Governance health
curl http://localhost:40000/api/health

# Dashboard
curl http://localhost:40000/dashboard

# Version gate status
curl http://localhost:40000/api/version-check/aming-claw

# Project registry
curl http://localhost:40000/api/projects

# Graph status
curl http://localhost:40000/api/graph-governance/aming-claw/status
```

### Executor Monitoring

Use MCP tools in Claude Code:
- `executor_status` — current state, tasks claimed, uptime
- `task_list` — all tasks with status
- `wf_summary` — node status counts

## 13. Known Issues

### Active

| Issue | Workaround |
|-------|------------|
| Gateway code changes need rebuild | `docker compose build telegram-gateway && up -d` |

### Historical (resolved by 2026-05-01 trailer-priority migration)

| Issue | Resolution |
|-------|------------|
| DB lock after version-update | `/api/version-update` is deprecated and no longer writes DB state — the lock path is gone. |
| VERSION file lags 1 commit | `chain_version` is derived from the git `Chain-Source-Stage` trailer; no VERSION file involved. |
| Dirty workspace gate false positive | `.claude/` filter (D5) + short/full hash prefix-match (B35) ship in current main. |

## 14. Data Persistence

| Data | Location | Backup Strategy |
|------|----------|----------------|
| Governance DB | `governance.db` (SQLite) | Git-tracked or periodic backup |
| Memory records | `governance.db` memories table | Included in DB backup |
| Task history | `governance.db` tasks table | Included in DB backup |
| Audit log | `governance.db` audit table | Included in DB backup |
| Redis data | Docker volume | Ephemeral (24h TTL), no backup needed |
| Git repo | `.git/` | Standard git remote push |
