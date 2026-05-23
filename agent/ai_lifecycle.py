"""AI Lifecycle Manager — v6 Executor-driven architecture.

All AI process management goes through this module.
AI cannot start AI. Only Executor code can create sessions.

Usage:
    manager = AILifecycleManager()
    session = manager.create_session(
        role="coordinator", prompt="...", context={...},
        project_id="amingClaw", timeout_sec=120
    )
    output = manager.wait_for_output(session.session_id)
    # output is structured JSON (parsed by ai_output_parser)
"""

import json
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Heartbeat constants — used by the streaming watchdog in create_session._run().
# Claude often stays silent while a Bash tool is running, so the default
# no-output window is intentionally longer than a short HTTP/task lease.
_HANG_TIMEOUT = _int_env("AI_HANG_TIMEOUT_SEC", 300)
_COORDINATOR_HANG_TIMEOUT = _int_env("AI_COORDINATOR_HANG_TIMEOUT_SEC", 300)
_MAX_TIMEOUT = _int_env("AI_MAX_TIMEOUT_SEC", 1200)
_DEFAULT_CLAUDE_ROLE_TURN_CAPS = {
    "coordinator": "1",
    "pm": "60",
    "dev": "40",
    "qa": "40",
    "gatekeeper": "20",
}


def _build_turn_caps():
    """Build turn caps from YAML configs with fallback to defaults."""
    try:
        from agent.governance.role_config import get_all_role_configs
        configs = get_all_role_configs()
        if configs:
            result = {}
            for role_name, config in configs.items():
                result[role_name] = str(config.max_turns)
            return result
    except Exception:
        pass
    return dict(_DEFAULT_CLAUDE_ROLE_TURN_CAPS)


_CLAUDE_ROLE_TURN_CAPS = _build_turn_caps()


def _kill_process_tree(pid: int) -> bool:
    """Best-effort process-tree termination for AI CLI sessions."""
    if not pid or pid <= 0:
        return False

    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except (ProcessLookupError, OSError):
            return False

    try:
        os.killpg(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except (ProcessLookupError, OSError):
            return False


@dataclass
class AISession:
    """Represents a running AI CLI process."""
    session_id: str
    role: str               # coordinator / dev / tester / qa
    pid: int                # OS process ID
    project_id: str
    prompt: str
    context: dict
    started_at: float       # time.time()
    timeout_sec: int
    status: str = "running"  # running / completed / failed / killed / timeout
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    last_heartbeat: float = field(default_factory=time.time)  # updated on each stdout line
    provider: str = ""
    model: str = ""
    workspace: str = ""
    log_path: str = ""
    input_path: str = ""
    output_path: str = ""
    prompt_file: str = ""


class AILifecycleManager:
    """Manages all AI CLI processes. Code-controlled, AI cannot self-start."""

    def __init__(self):
        self._sessions: dict[str, AISession] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _resolve_provider_model(role: str) -> tuple[str, str]:
        """Resolve provider/model for a role, defaulting to anthropic."""
        try:
            from pipeline_config import get_effective_pipeline_config, resolve_role_config
            config = get_effective_pipeline_config()
            resolved = resolve_role_config(role, config)
            provider = (resolved.get("provider") or "anthropic").strip().lower()
            model = (resolved.get("model") or "").strip()
            return provider or "anthropic", model
        except Exception:
            return "anthropic", ""

    @staticmethod
    def _allowed_tools_for_role(role: str) -> str:
        if role == "dev":
            return "Read,Grep,Glob,Write,Edit,Bash"
        if role == "tester":
            return "Read,Grep,Glob,Bash"
        if role == "gatekeeper":
            return "Read,Grep,Glob"
        if role == "pm":
            return "Read,Grep,Glob"
        if role == "coordinator":
            return ""
        return "Read,Grep,Glob"

    @staticmethod
    def _claude_turn_cap(role: str, context: Optional[dict] = None, prompt: str = "") -> str:
        base = _CLAUDE_ROLE_TURN_CAPS.get(role, "")
        if role != "dev":
            return base

        context = context or {}
        metadata = context.get("metadata", {}) if isinstance(context.get("metadata", {}), dict) else {}
        target_files = context.get("target_files", []) or []
        requirements = context.get("requirements", []) or []
        operation_type = (
            metadata.get("operation_type")
            or context.get("operation_type")
            or ""
        )
        replay_source = str(context.get("replay_source", "") or metadata.get("replay_source", "")).lower()

        is_heavy_workflow_task = (
            operation_type == "workflow_improvement"
            or "lane" in replay_source
            or len(target_files) >= 8
            or len(requirements) >= 6
            or len(prompt) >= 5000
        )
        return "60" if is_heavy_workflow_task else base

    @staticmethod
    def _build_claude_command(role: str, model: str, prompt_file: str, cwd: str = "", context: Optional[dict] = None, prompt: str = "") -> list[str]:
        claude_bin = os.getenv("CLAUDE_BIN", "claude")
        allowed_tools = AILifecycleManager._allowed_tools_for_role(role)
        cmd = [
            claude_bin,
            "-p",
            "--system-prompt-file", prompt_file,
        ]
        if model:
            cmd.extend(["--model", model])
        if cwd:
            cmd.extend(["--add-dir", cwd])
        if allowed_tools:
            cmd.extend(["--allowedTools", allowed_tools])
        max_turns = AILifecycleManager._claude_turn_cap(role, context=context, prompt=prompt)
        if max_turns:
            cmd.extend(["--max-turns", max_turns])
        return cmd

    @staticmethod
    def _build_codex_command(model: str, cwd: str) -> list[str]:
        codex_bin = os.getenv("CODEX_BIN", "").strip()
        if not codex_bin:
            codex_bin = "codex.cmd" if os.name == "nt" else "codex"
        dangerous = os.getenv("CODEX_DANGEROUS", "1").strip().lower() not in {"0", "false", "no"}
        cmd = [
            codex_bin,
            "exec",
            "--skip-git-repo-check",
            "-C",
            cwd,
            "-o",
        ]
        if dangerous:
            cmd.insert(2, "--dangerously-bypass-approvals-and-sandbox")
        else:
            cmd[2:2] = ["--sandbox", "workspace-write"]
        if model:
            cmd[2:2] = ["--model", model]
        return cmd

    @staticmethod
    def _compose_codex_prompt(system_prompt: str, prompt: str) -> str:
        return (
            "Follow this system instruction exactly.\n\n"
            "=== SYSTEM PROMPT START ===\n"
            f"{system_prompt}\n"
            "=== SYSTEM PROMPT END ===\n\n"
            "=== TASK PROMPT START ===\n"
            f"{prompt}\n"
            "=== TASK PROMPT END ===\n"
        )

    def create_session(
        self,
        role: str,
        prompt: str,
        context: dict,
        project_id: str,
        timeout_sec: int = 120,
        workspace: str = "",
    ) -> AISession:
        """Start an AI CLI process.

        Args:
            role: coordinator / dev / tester / qa
            prompt: The user message or task prompt
            context: Assembled context dict (injected as system prompt)
            project_id: Project identifier
            timeout_sec: Max execution time
            workspace: Working directory for the CLI

        Returns:
            AISession with PID and session_id
        """
        session_id = f"ai-{role}-{int(time.time())}-{uuid.uuid4().hex[:6]}"

        # File-based logging — log.info() blocks in MCP subprocess (IO pipe deadlock)
        _al_t0 = time.time()
        def _al_log(msg):
            try:
                al_path = os.path.join(workspace or os.getcwd(), "shared-volume", "codex-tasks", "logs",
                                       f"ai-lifecycle-{session_id}.txt")
                os.makedirs(os.path.dirname(al_path), exist_ok=True)
                with open(al_path, "a") as f:
                    f.write(f"{time.time()-_al_t0:.1f}s {msg}\n")
            except Exception:
                pass

        # Build system prompt from context
        system_prompt = self._build_system_prompt(role, prompt, context, project_id)
        _al_log(f"build_system_prompt: {len(system_prompt)} chars role={role}")

        # Audit: write prompt to Redis Stream for full round-trip tracking
        self._audit_prompt(session_id, role, project_id, workspace or "", prompt, system_prompt)
        _al_log("audit_prompt done")

        _provider, _model = self._resolve_provider_model(role)
        _al_log(f"pipeline_config: role={role} provider={_provider} model={_model}")

        cwd = workspace or os.getenv("CODEX_WORKSPACE", os.getcwd())
        log_dir = os.path.join(workspace or os.getcwd(), "shared-volume", "codex-tasks", "logs")
        os.makedirs(log_dir, exist_ok=True)
        output_last = os.path.join(log_dir, f"last-message-{session_id.replace('ai-','')}.txt")
        lifecycle_log_path = os.path.join(log_dir, f"ai-lifecycle-{session_id}.txt")
        input_path = os.path.join(log_dir, f"input-{session_id.replace('ai-','')}.txt")
        output_path = os.path.join(log_dir, f"output-{session_id.replace('ai-','')}.txt")

        # Write system prompt to file
        prompt_file = os.path.join(tempfile.gettempdir(), f"ctx-{session_id}.md")
        try:
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(system_prompt)
            _al_log(f"prompt_file: {prompt_file} ({len(system_prompt)} bytes)")
        except Exception as e:
            _al_log(f"ERROR prompt_file write failed: {e}")

        provider = _provider if _provider in ("anthropic", "openai") else "anthropic"
        if provider == "openai":
            cmd = self._build_codex_command(_model, cwd)
            cmd.append(output_last)
            composed_prompt = self._compose_codex_prompt(system_prompt, prompt)
            stdin_prompt = composed_prompt
        else:
            cmd = self._build_claude_command(role, _model, prompt_file, cwd, context=context, prompt=prompt)
            stdin_prompt = prompt

        # Strip env vars that cause nested Claude issues
        env = dict(os.environ)
        if provider == "anthropic":
            env = {k: v for k, v in env.items()
                   if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                                "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
                                "CLAUDE_CODE_EXECPATH",
                                "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH",
                                "CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES",
                                "CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL",
                                "CLAUDE_CODE_OAUTH_TOKEN")}
            env.pop("ANTHROPIC_API_KEY", None)

        # Create session object first (wait_for_output polls session.status)
        # NOTE: pid=0 is a sentinel — do NOT log session.pid until pid != 0
        # (Popen assigns the real PID at line ~326).  Any log line referencing
        # pid=0 looks like a real process and confuses crash-recovery grep.
        session = AISession(
            session_id=session_id,
            role=role,
            pid=0,  # set after subprocess starts
            project_id=project_id,
            prompt=prompt,
            context=context,
            started_at=time.time(),
            timeout_sec=timeout_sec,
            provider=provider,
            model=_model,
            workspace=cwd,
            log_path=lifecycle_log_path,
            input_path=input_path,
            output_path=output_path,
            prompt_file=prompt_file,
        )
        with self._lock:
            self._sessions[session_id] = session
            if session.pid != 0:
                _al_log(f"Session registered: sid={session_id} pid={session.pid}")
            # else: pid==0, suppress pid-dependent log until Popen assigns real PID

        # Run CLI in a background thread using Popen and pipe reader threads.
        # This keeps stdout/stderr drained while the process runs, allowing the
        # heartbeat watchdog to kill silent sessions instead of waiting for the
        # absolute communicate() timeout.
        def _run():
            try:
                # Save input for replay/debug
                try:
                    os.makedirs(os.path.dirname(input_path), exist_ok=True)
                    with open(input_path, "w", encoding="utf-8") as _f:
                        _f.write(f"=== SYSTEM PROMPT ({len(system_prompt)} chars) ===\n")
                        _f.write(system_prompt)
                        _f.write(f"\n\n=== STDIN PROMPT ({len(stdin_prompt)} chars) ===\n")
                        _f.write(stdin_prompt)
                        _f.write(f"\n\n=== CLI CMD ===\n")
                        _f.write(" ".join(cmd))
                except Exception:
                    pass

                _al_log(f"Popen starting: {' '.join(cmd[:6])}...")
                popen_kwargs = dict(
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=cwd,
                    env=env,
                )
                # On Windows, create a new process group for clean tree-kill
                if os.name == "nt":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_kwargs["start_new_session"] = True
                proc = subprocess.Popen(cmd, **popen_kwargs)
                session.pid = proc.pid
                _al_log(f"Popen started: pid={proc.pid}")

                stdout_parts: list[str] = []
                stderr_parts: list[str] = []
                io_lock = threading.Lock()

                def _read_pipe(pipe, parts: list[str], label: str) -> None:
                    try:
                        while True:
                            chunk = pipe.readline()
                            if chunk == "":
                                break
                            with io_lock:
                                parts.append(chunk)
                            session.last_heartbeat = time.time()
                    except Exception as exc:
                        with io_lock:
                            parts.append(f"\n[{label} reader error: {exc}]\n")

                stdout_thread = threading.Thread(
                    target=_read_pipe, args=(proc.stdout, stdout_parts, "stdout"), daemon=True
                )
                stderr_thread = threading.Thread(
                    target=_read_pipe, args=(proc.stderr, stderr_parts, "stderr"), daemon=True
                )
                stdout_thread.start()
                stderr_thread.start()

                try:
                    if proc.stdin:
                        proc.stdin.write(stdin_prompt)
                        proc.stdin.close()
                except Exception as exc:
                    _al_log(f"stdin write failed: {exc}")

                max_timeout = min(_MAX_TIMEOUT, int(session.timeout_sec or _MAX_TIMEOUT))
                hang_timeout = _COORDINATOR_HANG_TIMEOUT if role == "coordinator" else _HANG_TIMEOUT
                timeout_reason = ""
                while proc.poll() is None:
                    now = time.time()
                    if now - session.started_at > max_timeout:
                        timeout_reason = f"max runtime exceeded after {max_timeout}s"
                        break
                    if now - session.last_heartbeat > hang_timeout:
                        timeout_reason = f"no CLI output for {hang_timeout}s"
                        break
                    time.sleep(0.5)

                if timeout_reason:
                    _al_log(f"Popen watchdog timeout: {timeout_reason}")
                    _kill_process_tree(proc.pid or session.pid)
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                else:
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass

                stdout_thread.join(timeout=2)
                stderr_thread.join(timeout=2)

                with io_lock:
                    session.stdout = "".join(stdout_parts)
                    session.stderr = "".join(stderr_parts)
                if timeout_reason:
                    session.exit_code = -1
                    session.status = "timeout"
                    session.stderr = (session.stderr + f"\n{timeout_reason}").strip()
                else:
                    session.exit_code = proc.returncode
                    session.status = "completed" if proc.returncode == 0 else "failed"
                if provider == "openai" and os.path.exists(output_last):
                    try:
                        session.stdout = Path(output_last).read_text(encoding="utf-8")
                    except Exception:
                        pass
                _al_log(f"Popen done: rc={proc.returncode} stdout={len(session.stdout)} stderr={len(session.stderr)}")
                # Save output for debug
                try:
                    with open(output_path, "w", encoding="utf-8") as _f:
                        _f.write(f"=== STATUS: {session.status} rc={proc.returncode} elapsed={time.time()-session.started_at:.1f}s ===\n\n")
                        _f.write(f"=== STDOUT ({len(session.stdout)} chars) ===\n")
                        _f.write(session.stdout)
                        if session.stderr:
                            _f.write(f"\n\n=== STDERR ({len(session.stderr)} chars) ===\n")
                            _f.write(session.stderr)
                except Exception:
                    pass
            except subprocess.TimeoutExpired:
                session.status = "timeout"
                session.exit_code = -1
                session.stdout = ""
                session.stderr = "Timeout exceeded"
                _al_log(f"Popen TIMEOUT after {_MAX_TIMEOUT}s")
                # Kill the process on timeout
                try:
                    _kill_process_tree(proc.pid or session.pid)
                    proc.communicate(timeout=5)
                except Exception:
                    pass
            except FileNotFoundError:
                session.status = "failed"
                session.exit_code = -1
                session.stdout = ""
                session.stderr = f"CLI not found: {cmd[0]}"
                _al_log(f"Popen ERROR: CLI not found: {cmd[0]}")
            except Exception as e:
                session.status = "failed"
                session.exit_code = -1
                session.stdout = ""
                session.stderr = str(e)
                _al_log(f"Popen ERROR: {e}")
            finally:
                # Cleanup prompt file
                try:
                    if os.path.exists(prompt_file):
                        os.remove(prompt_file)
                except Exception:
                    pass

        _al_log(f"session_created: {session_id} timeout={timeout_sec}s")
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        return session

    def wait_for_output(self, session_id: str, poll_interval: float = 0.5) -> dict:
        """Wait for AI session to complete and return output.

        Returns:
            {"status": "completed|failed|timeout", "stdout": "...", "stderr": "...",
             "exit_code": 0, "elapsed_sec": 12.3}
        """
        session = self._sessions.get(session_id)
        if not session:
            return {"status": "failed", "error": f"session {session_id} not found"}

        # Wait until session is no longer running.
        # The watchdog inside _run() is the primary enforcer; this is a safety fallback.
        while session.status == "running":
            elapsed = time.time() - session.started_at
            if elapsed > _MAX_TIMEOUT + 30:
                self.kill_session(session_id, "timeout exceeded in wait")
                break
            time.sleep(poll_interval)

        elapsed = time.time() - session.started_at

        result = {
            "status": session.status,
            "stdout": session.stdout,
            "stderr": session.stderr,
            "exit_code": session.exit_code,
            "elapsed_sec": round(elapsed, 1),
            "session_id": session_id,
            "role": session.role,
            "provider": session.provider,
            "model": session.model,
            "workspace": session.workspace,
            "pid": session.pid,
            "log_path": session.log_path,
            "input_path": session.input_path,
            "output_path": session.output_path,
        }
        self.audit_result(session_id, session.project_id, result)
        return result

    def kill_session(self, session_id: str, reason: str = "") -> bool:
        """Force-terminate an AI process (tree-kill on Windows)."""
        session = self._sessions.get(session_id)
        if not session or session.pid == 0:
            return False

        try:
            pid = session.pid
            killed = _kill_process_tree(pid)
            if killed:
                session.status = "killed"
            return killed
        except (ProcessLookupError, OSError):
            return False

    def cleanup_expired(self) -> int:
        """Kill all sessions that are hung (no heartbeat) or exceeded max total runtime."""
        killed = 0
        now = time.time()
        with self._lock:
            for sid, session in list(self._sessions.items()):
                if session.status != "running":
                    continue
                if now - session.started_at > _MAX_TIMEOUT:
                    self.kill_session(sid, "max_timeout_cleanup")
                    killed += 1
                elif now - session.last_heartbeat > _HANG_TIMEOUT:
                    self.kill_session(sid, "hang_timeout_cleanup")
                    killed += 1
        return killed

    def extend_deadline(self, session_id: str) -> None:
        """Reset the heartbeat clock for a running session.

        Call this from executor update_progress() hooks so that tasks actively
        reporting progress are not mistaken for hung processes.  Effective
        extension is +120 s from now (up to _MAX_TIMEOUT absolute cap).
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session and session.status == "running":
                session.last_heartbeat = time.time()
                log.debug("Heartbeat extended for session %s", session_id)

    def list_active(self) -> list[dict]:
        """List all active sessions."""
        result = []
        for session in self._sessions.values():
            if session.status == "running":
                result.append({
                    "session_id": session.session_id,
                    "role": session.role,
                    "pid": session.pid,
                    "project_id": session.project_id,
                    "elapsed_sec": round(time.time() - session.started_at, 1),
                })
        return result

    def get_session(self, session_id: str) -> Optional[AISession]:
        return self._sessions.get(session_id)

    def _build_system_prompt(self, role: str, prompt: str, context: dict, project_id: str) -> str:
        """Build the full prompt sent to Claude CLI.

        Structure:
          1. Role prompt (static, from ROLE_PROMPTS)
          2. API reference (shared across all roles)
          3. Base context snapshot (from /api/context-snapshot)
          4. Workspace info (dev only)
          5. Task prompt
        """
        from role_permissions import ROLE_PROMPTS, _API_REFERENCE

        role_prompt = ROLE_PROMPTS.get(role, ROLE_PROMPTS.get("coordinator", ""))

        # Fetch base context snapshot (single API call, consistent)
        # Coordinator: context is pre-injected by executor._build_prompt, skip snapshot fetch
        snapshot_str = ""
        if role != "coordinator":
            try:
                gov_url = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
                task_id = context.get("task_id", "")
                url = f"{gov_url}/api/context-snapshot/{project_id}?role={role}&task_id={task_id}"
                req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    snapshot = json.loads(resp.read().decode())
                if snapshot:
                    snapshot_str = (
                        "\n--- Base Context Snapshot ---\n"
                        f"{json.dumps(snapshot, ensure_ascii=False, indent=2)}\n"
                    )
            except Exception:
                pass  # context snapshot fetch failed — non-critical

        # Dev role: inject workspace and target_files so AI knows where to work
        workspace_info = ""
        if role == "dev":
            ws = context.get("workspace", "")
            tf = context.get("target_files", [])
            if ws:
                workspace_info = (
                    f"IMPORTANT: Your working directory is: {ws}\n"
                    f"All file paths MUST use this directory as root. "
                    f"Use absolute paths starting with {ws}/ for all Read/Write/Edit operations.\n"
                )
            if tf:
                workspace_info += f"Target files: {', '.join(tf)}\n"

        # Skip API reference for coordinator (no tools, can't call APIs)
        api_section = f"{_API_REFERENCE}\n\n" if role != "coordinator" else ""

        return (
            f"{role_prompt}\n\n"
            f"{api_section}"
            f"Project: {project_id}\n"
            f"{workspace_info}"
            f"{snapshot_str}\n"
            f"Task: {prompt}\n\n"
            "Respond with your decision in the specified JSON format."
        )

    @staticmethod
    def _audit_prompt(session_id: str, role: str, project_id: str,
                      workspace: str, prompt: str, system_prompt: str):
        """Write AI prompt to Redis Stream for audit trail."""
        try:
            from governance.redis_client import get_redis
            r = get_redis()
            if not r:
                return
            stream_key = f"ai:prompt:{session_id}"
            r.xadd(stream_key, {
                "type": "prompt",
                "session_id": session_id,
                "role": role,
                "project_id": project_id,
                "workspace": workspace,
                "prompt_length": str(len(prompt)),
                "system_prompt_length": str(len(system_prompt)),
                "user_prompt": prompt[:5000],  # Truncate for Redis memory
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }, maxlen=5000)
        except Exception as e:
            log.debug("Redis audit write failed (non-fatal): %s", e)

    @staticmethod
    def audit_result(session_id: str, project_id: str, result: dict):
        """Write AI result to Redis Stream for full round-trip audit."""
        try:
            from governance.redis_client import get_redis
            r = get_redis()
            if not r:
                return
            stream_key = f"ai:prompt:{session_id}"
            r.xadd(stream_key, {
                "type": "result",
                "session_id": session_id,
                "project_id": project_id,
                "status": result.get("status", "unknown"),
                "exit_code": str(result.get("exit_code", -1)),
                "elapsed_sec": str(result.get("elapsed_sec", 0)),
                "stdout_length": str(len(result.get("stdout", ""))),
                "stdout": result.get("stdout", "")[:10000],
                "stderr": result.get("stderr", "")[:2000],
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }, maxlen=5000)
        except Exception as e:
            log.debug("Redis audit result write failed (non-fatal): %s", e)
