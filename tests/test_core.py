from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from remote_agent.app_server import AppServer, AppServerError
from remote_agent.approvals import ApprovalManager
from remote_agent.bridge import BridgeBot, _split_message
from remote_agent.config import Settings
from remote_agent.session import SessionStore


class CoreTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self) -> BridgeBot:
        settings = Settings(
            "token", 5, 6, frozenset({7}), Path.cwd(), Path(".state"), Path(".codex-home"),
            "codex", "gpt-6-luna", 120, 1000,
        )
        bot = BridgeBot(settings)
        bot.is_ready = lambda: True  # type: ignore[method-assign]
        bot.is_closed = lambda: False  # type: ignore[method-assign]
        return bot

    def make_context(self, user_id: int = 7) -> SimpleNamespace:
        return SimpleNamespace(
            guild=SimpleNamespace(id=5),
            channel=SimpleNamespace(id=6),
            author=SimpleNamespace(id=user_id, bot=False),
            message=SimpleNamespace(webhook_id=None),
        )

    async def test_approval_is_origin_bound_single_use_and_deadline_bound(self) -> None:
        approvals = ApprovalManager()
        ticket = approvals.create(
            "request-1", "item/commandExecution/requestApproval", "item-1", "thread-1", "turn-1",
            5, 6, 7, "command", "full payload", 60,
        )
        self.assertFalse(approvals.resolve(ticket.code, True, 5, 6, 8))
        self.assertFalse(approvals.resolve(ticket.code, True, 5, 9, 7))
        self.assertTrue(approvals.resolve(ticket.code, True, 5, 6, 7))
        self.assertFalse(approvals.resolve(ticket.code, True, 5, 6, 7))
        self.assertTrue(await approvals.wait(ticket, 60))

        expired = approvals.create("r2", "method", "i2", "t", "u", 5, 6, 7, "title", "detail", 0)
        expired.deadline = 0
        self.assertFalse(approvals.resolve(expired.code, True, 5, 6, 7))
        self.assertFalse(await approvals.wait(expired, 1))

    async def test_deny_all_resolves_pending_as_denied(self) -> None:
        approvals = ApprovalManager()
        ticket = approvals.create("r", "method", "i", "t", "u", 1, 2, 3, "title", "detail", 60)
        approvals.deny_all()
        self.assertFalse(await approvals.wait(ticket, 60))
        self.assertFalse(approvals.resolve(ticket.code, True, 1, 2, 3))

    async def test_session_is_scoped_and_never_replays_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            store = SessionStore(path, {"channel_id": "1", "model": "gpt-6-luna"})
            store.save("thr_abc")
            self.assertEqual(store.load(), "thr_abc")
            wrong = SessionStore(path, {"channel_id": "2", "model": "gpt-6-luna"})
            self.assertIsNone(wrong.load())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["thread_id"], "thr_abc")

    async def test_new_thread_is_already_attached_and_not_resumed(self) -> None:
        bot = self.make_bot()
        bot.thread_id = "thr_current"
        bot._thread_attached = True

        async def unexpected_rpc(*_args: object, **_kwargs: object) -> dict:
            raise AssertionError("thread/start result must not be resumed in the same process")

        bot.server.rpc = unexpected_rpc  # type: ignore[method-assign]
        self.assertEqual(await bot._ensure_thread(), "thr_current")

    async def test_saved_empty_thread_rollout_can_be_replaced_safely(self) -> None:
        bot = self.make_bot()
        with tempfile.TemporaryDirectory() as tmp:
            bot.sessions = SessionStore(Path(tmp) / "session.json", {"test": "scope"})
            bot.sessions.save("thr_before_first_turn", has_turn=False)
            bot.thread_id = bot.sessions.load()
            bot._thread_attached = False

            class FakeServer:
                async def rpc(self, method: str, _params: dict) -> dict:
                    if method == "thread/resume":
                        raise AppServerError("no rollout found for thread id")
                    return {
                        "thread": {"id": "thr_new"},
                        "approvalPolicy": "untrusted",
                        "approvalsReviewer": "user",
                        "sandbox": {"type": "readOnly"},
                    }

            bot.server = FakeServer()  # type: ignore[assignment]
            self.assertEqual(await bot._ensure_thread(), "thr_new")
            self.assertTrue(bot._thread_attached)
            self.assertFalse(bot.sessions.has_turn)

    async def test_dedicated_codex_home_rejects_project_config_and_custom_extensions(self) -> None:
        async def event_handler(_method: str, _params: dict) -> None:
            return None

        async def request_handler(_message: dict) -> dict:
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "isolated"
            work = root / "work"
            work.mkdir()
            server = AppServer("codex", home, work, event_handler, request_handler)
            server._write_isolated_config()
            (home / "skills" / ".system").mkdir(parents=True)
            server._write_isolated_config()
            (home / "skills" / "custom").mkdir()
            with self.assertRaises(AppServerError):
                server._write_isolated_config()
            (home / "skills" / "custom").rmdir()
            (work / ".codex").mkdir()
            (work / ".codex" / "config.toml").write_text("[mcp_servers.evil]\n", encoding="utf-8")
            with self.assertRaises(AppServerError):
                server._write_isolated_config()

    async def test_rpc_ids_route_concurrent_responses_and_preserve_order(self) -> None:
        class Writer:
            def __init__(self) -> None:
                self.lines: list[bytes] = []

            def write(self, data: bytes) -> None:
                self.lines.append(data)

            async def drain(self) -> None:
                return None

        class Process:
            def __init__(self) -> None:
                self.returncode = None
                self.stdout = asyncio.StreamReader()
                self.stdin = Writer()

            def kill(self) -> None:
                self.returncode = -9

            async def wait(self) -> int:
                return -9

        observed: list[str] = []

        async def event_handler(method: str, _params: dict) -> None:
            observed.append(method)

        async def request_handler(_message: dict) -> dict:
            return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            server = AppServer("codex", Path(tmp) / "home", Path(tmp), event_handler, request_handler)
            process = Process()
            server.process = process  # type: ignore[assignment]
            server._event_task = asyncio.create_task(server._event_loop())
            server._reader_task = asyncio.create_task(server._read_loop())
            first = asyncio.create_task(server.rpc("one"))
            second = asyncio.create_task(server.rpc("two"))
            await asyncio.sleep(0)
            requests = [json.loads(line) for line in process.stdin.lines]
            process.stdout.feed_data((json.dumps({"id": requests[1]["id"], "result": {"v": 2}}) + "\n").encode())
            process.stdout.feed_data((json.dumps({"id": requests[0]["id"], "result": {"v": 1}}) + "\n").encode())
            self.assertEqual(await first, {"v": 1})
            self.assertEqual(await second, {"v": 2})
            process.stdout.feed_data((json.dumps({"method": "item/started", "params": {}}) + "\n").encode())
            await asyncio.wait_for(server._events.join(), timeout=1)
            self.assertEqual(observed, ["item/started"])
            server._reader_task.cancel()
            await asyncio.gather(server._reader_task, return_exceptions=True)
            server._event_task.cancel()
            await asyncio.gather(server._event_task, return_exceptions=True)

    async def test_bridge_policy_check_requires_all_confirmed_settings(self) -> None:
        correct = {"approvalPolicy": "untrusted", "approvalsReviewer": "user", "sandbox": {"type": "readOnly"}}
        BridgeBot._verify_policy(correct)
        for wrong in (
            {**correct, "approvalPolicy": "on-request"},
            {**correct, "approvalsReviewer": "auto_review"},
            {**correct, "sandbox": {"type": "dangerFullAccess"}},
            {"thread": {"approvalPolicy": "untrusted"}},
        ):
            with self.assertRaises(AppServerError):
                BridgeBot._verify_policy(wrong)

    async def test_approval_detail_requires_full_cached_diff_or_command(self) -> None:
        settings = Settings(
            "token", 1, 2, frozenset({3}), Path.cwd(), Path(".state"), Path(".codex-home"),
            "codex", "gpt-6-luna", 120, 1000,
        )
        bot = BridgeBot(settings)
        title, detail = bot.approval_detail("item/fileChange/requestApproval", {}, "item-1")
        self.assertEqual(detail, "")
        bot._file_items["item-1"] = {"type": "fileChange", "changes": [{"path": "x.py", "diff": "-a\n+b"}]}
        title, detail = bot.approval_detail("item/fileChange/requestApproval", {"reason": "write"}, "item-1")
        self.assertIn("x.py", detail)
        self.assertIn("-a", detail)
        bot._file_items["cmd-1"] = {"command": ["rm", "file"], "cwd": "/work"}
        _, detail = bot.approval_detail("item/commandExecution/requestApproval", {}, "cmd-1")
        self.assertIn('"rm"', detail)
        self.assertIn("/work", detail)
        await bot.close()

    async def test_discord_authorization_requires_exact_guild_channel_and_user(self) -> None:
        bot = self.make_bot()
        replied: list[str] = []

        async def reply(_ctx: object, content: str) -> None:
            replied.append(content)

        bot.reply = reply  # type: ignore[method-assign]
        self.assertTrue(await bot.authorized(self.make_context()))
        self.assertFalse(await bot.authorized(self.make_context(user_id=8)))
        wrong_channel = self.make_context()
        wrong_channel.channel.id = 99
        self.assertFalse(await bot.authorized(wrong_channel))
        self.assertGreaterEqual(len(replied), 2)

    async def test_stop_race_converts_click_to_denial(self) -> None:
        bot = self.make_bot()
        ctx = self.make_context()
        bot.active_context = ctx  # type: ignore[assignment]
        bot.active_turn_id = "turn-1"
        bot.thread_id = "thread-1"
        bot._stopping = True
        bot._file_items["item-1"] = {"type": "commandExecution", "command": ["rm", "x"], "cwd": "/work"}

        async def publish(_ctx: object, ticket: object) -> bool:
            ticket_manager = bot.approvals
            ticket_manager.resolve(ticket.code, True, 5, 6, 7)
            return True

        bot.publish_approval = publish  # type: ignore[method-assign]
        result = await bot.on_server_request({
            "id": 12,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1"},
        })
        self.assertEqual(result, {"decision": "decline"})

    async def test_disconnect_denies_pending_approval(self) -> None:
        bot = self.make_bot()
        ctx = self.make_context()
        bot.active_context = ctx  # type: ignore[assignment]
        bot.active_turn_id = "turn-1"
        bot.thread_id = "thread-1"
        bot._file_items["item-1"] = {"type": "commandExecution", "command": ["rm", "x"], "cwd": "/work"}
        published = asyncio.Event()

        async def publish(_ctx: object, _ticket: object) -> bool:
            published.set()
            return True

        bot.publish_approval = publish  # type: ignore[method-assign]
        request = asyncio.create_task(bot.on_server_request({
            "id": 13,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1"},
        }))
        await asyncio.wait_for(published.wait(), timeout=1)
        await bot.on_disconnect()
        result = await asyncio.wait_for(request, timeout=1)
        self.assertEqual(result, {"decision": "decline"})

    async def test_split_message_preserves_complete_text(self) -> None:
        text = "line\n" * 800
        chunks = _split_message(text, 64)
        self.assertTrue(all(len(chunk) <= 64 for chunk in chunks))
        self.assertEqual("".join(chunks), text)


if __name__ == "__main__":
    unittest.main()
