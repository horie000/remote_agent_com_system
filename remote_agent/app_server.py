from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable


EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
ServerRequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class AppServerError(RuntimeError):
    pass


class AppServer:
    """Small JSON-RPC stdio client for Codex App Server."""

    def __init__(
        self,
        executable: str,
        codex_home: Path,
        workdir: Path,
        event_handler: EventHandler,
        request_handler: ServerRequestHandler,
    ) -> None:
        self.executable = executable
        self.codex_home = codex_home
        self.workdir = workdir.resolve()
        self.event_handler = event_handler
        self.request_handler = request_handler
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._dead = asyncio.Event()
        self._events: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue(maxsize=256)
        self._event_task: asyncio.Task[None] | None = None
        self._request_tasks: set[asyncio.Task[None]] = set()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail = bytearray()
        self._log = logging.getLogger("remote_agent.app_server")

    async def start(self) -> None:
        if self.process is not None:
            return
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self._write_isolated_config()
        child_env = os.environ.copy()
        for key in tuple(child_env):
            if key.upper().startswith("DISCORD_"):
                child_env.pop(key, None)
        child_env["CODEX_HOME"] = str(self.codex_home)
        disabled_features = (
            "apps", "hooks", "browser_use", "browser_use_external",
            "browser_use_full_cdp_access", "computer_use", "in_app_browser",
            "remote_plugin", "skill_mcp_dependency_install", "skill_search",
            "workspace_dependencies", "image_generation", "multi_agent",
        )
        args = [self.executable, "app-server", "--stdio"]
        for feature in disabled_features:
            args.extend(("--disable", feature))
        self.process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
            limit=8 * 1024 * 1024,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="codex-app-server-stderr")
        self._event_task = asyncio.create_task(self._event_loop(), name="codex-app-server-events")
        self._reader_task = asyncio.create_task(self._read_loop(), name="codex-app-server-reader")
        try:
            await self.rpc("initialize", {
                "clientInfo": {"name": "remote_agent_com_sys", "title": "Discord Codex Bridge", "version": "0.1.0"}
            }, timeout=30)
            await self.notify("initialized", {})
        except Exception:
            await asyncio.sleep(0.05)
            diagnostic = self._safe_stderr()
            if diagnostic:
                self._log.error("Codex App Server startup diagnostic: %s", diagnostic)
            raise

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                chunk = await process.stderr.read(1024)
                if not chunk:
                    return
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > 8192:
                    del self._stderr_tail[:-8192]
        except asyncio.CancelledError:
            raise

    def _safe_stderr(self) -> str:
        text = self._stderr_tail.decode("utf-8", errors="replace")[-3000:].strip()
        text = re.sub(r"(?i)(token|secret|api[_-]?key|password|credential)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED_KEY]", text)
        text = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b", "[REDACTED_TOKEN]", text)
        text = re.sub(r"\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b", "[REDACTED_TOKEN]", text)
        return text

    def _write_isolated_config(self) -> None:
        # CODEX_HOME is dedicated to this bridge, so user MCP servers/hooks are not inherited.
        # This config also makes a safe default if a thread omits explicit overrides.
        self.codex_home.mkdir(parents=True, exist_ok=True)
        config = self.codex_home / "config.toml"
        text = (
            "# Managed by remote_agent_com_sys; keep tool extensions disabled.\n"
            'approval_policy = "on-request"\n'
            'sandbox_mode = "read-only"\n'
            'approvals_reviewer = "user"\n'
            'web_search = "disabled"\n'
        )
        legacy_bridge_config = text.replace('approval_policy = "on-request"', 'approval_policy = "untrusted"')
        current_config = config.read_text(encoding="utf-8") if config.exists() else None
        if current_config is not None and current_config not in {text, legacy_bridge_config}:
            raise AppServerError("dedicated CODEX_HOME config.toml changed; restore bridge defaults to disable hooks and MCP tools")
        if current_config != text:
            config.write_text(text, encoding="utf-8")
        for directory in ("skills", "plugins", "apps", "marketplaces"):
            if directory == "skills":
                skills = self.codex_home / directory
                if skills.exists() and any(child.name != ".system" for child in skills.iterdir()):
                    raise AppServerError("dedicated CODEX_HOME contains custom skills")
            elif (self.codex_home / directory).exists():
                raise AppServerError(f"dedicated CODEX_HOME must not contain custom {directory}")
        for parent in (self.workdir, *self.workdir.parents):
            candidate = parent / ".codex" / "config.toml"
            if candidate == Path.home() / ".codex" / "config.toml":
                continue
            if candidate.exists():
                raise AppServerError(f"project Codex config is not allowed for remote turns: {candidate}")

    async def rpc(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60) -> dict[str, Any]:
        if self.process is None or self.process.returncode is not None or self.process.stdin is None:
            raise AppServerError("Codex App Server is not running")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._write(message)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def _event_loop(self) -> None:
        while True:
            item = await self._events.get()
            try:
                if item is None:
                    return
                await self.event_handler(*item)
            except Exception:
                self._log.exception("Codex App Server notification handler failed")
                try:
                    await self.request_handler({"method": "__eventerror__", "id": None, "params": {}})
                except Exception:
                    pass
            finally:
                self._events.task_done()

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        await self._write({"id": request_id, "result": result})

    async def _write(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AppServerError("Codex App Server disconnected")
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            process.stdin.write(data)
            await process.stdin.drain()

    async def _read_loop(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AppServerError("Codex App Server emitted invalid JSON") from exc
                if "id" in message and ("result" in message or "error" in message):
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(AppServerError(str(message["error"].get("message", "RPC failed"))))
                        else:
                            future.set_result(message.get("result") or {})
                elif "id" in message and "method" in message:
                    task = asyncio.create_task(self._handle_ordered_request(message))
                    self._request_tasks.add(task)
                    task.add_done_callback(self._request_tasks.discard)
                elif isinstance(message.get("method"), str):
                    try:
                        self._events.put_nowait((message["method"], message.get("params") or {}))
                    except asyncio.QueueFull as exc:
                        raise AppServerError("Codex notification queue overflow; stopping safely") from exc
            await asyncio.wait_for(self._events.join(), timeout=5)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log.exception("Codex App Server reader failed")
        finally:
            self._dead.set()
            self._fail_pending()
            if process.returncode is None:
                process.kill()
                await process.wait()
            try:
                await self.request_handler({"method": "__disconnect__", "id": None, "params": {}})
            except Exception:
                pass

    async def _handle_ordered_request(self, message: dict[str, Any]) -> None:
        try:
            await self._events.join()
            await self._handle_server_request(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log.exception("ordered server request failed")

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        try:
            result = await self.request_handler(message)
            await self.respond(message["id"], result)
        except Exception:
            # Unsupported or failed requests are answered with an empty result only when
            # their protocol handler defines a safe denial. Otherwise the turn fails closed.
            self._log.exception("server-initiated request could not be handled")
            try:
                await self._write({
                    "id": message.get("id"),
                    "error": {"code": -32603, "message": "Request denied by bridge"},
                })
            except Exception:
                pass

    def _fail_pending(self) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(AppServerError("Codex App Server disconnected"))

    async def wait_dead(self) -> None:
        await self._dead.wait()

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        for task in list(self._request_tasks):
            task.cancel()
        await asyncio.gather(*self._request_tasks, return_exceptions=True)
        if self._event_task is not None:
            self._event_task.cancel()
            await asyncio.gather(self._event_task, return_exceptions=True)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
        self.process = None
