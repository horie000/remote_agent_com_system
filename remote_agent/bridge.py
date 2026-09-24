from __future__ import annotations

import asyncio
import io
import json
import logging
from typing import Any

import discord
from discord.ext import commands

from .app_server import AppServer, AppServerError
from .approvals import ApprovalManager, ApprovalTicket
from .audit import AuditLog
from .config import Settings
from .session import SessionStore


MAX_DISCORD_MESSAGE = 1900
APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _split_message(text: str, limit: int = MAX_DISCORD_MESSAGE) -> list[str]:
    if not text:
        return ["(応答なし)"]
    chunks: list[str] = []
    while len(text) > limit:
        split = text.rfind("\n", 0, limit)
        if split < limit // 2:
            split = limit
        else:
            split += 1
        chunks.append(text[:split])
        text = text[split:]
    if text:
        chunks.append(text)
    return chunks


class ApprovalView(discord.ui.View):
    def __init__(self, bridge: "BridgeBot", ticket: ApprovalTicket) -> None:
        super().__init__(timeout=bridge.settings.approval_timeout)
        self.bridge = bridge
        self.ticket = ticket

    async def _resolve(self, interaction: discord.Interaction, approve: bool) -> None:
        guild_id = interaction.guild_id or 0
        channel_id = interaction.channel_id or 0
        user_id = interaction.user.id
        accepted = self.bridge.approvals.resolve(
            self.ticket.code, approve, guild_id, channel_id, user_id
        )
        if accepted:
            self.bridge.audit.write(
                "approval_decided",
                request_id=self.ticket.request_id,
                thread_id=self.ticket.thread_id,
                turn_id=self.ticket.turn_id,
                user_id=user_id,
                channel_id=channel_id,
                decision="approved_once" if approve else "denied",
            )
            await interaction.response.send_message(
                "1回限りの承認を受け付けました。" if approve else "拒否しました。",
                ephemeral=True,
            )
            self.stop()
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except (discord.HTTPException, AttributeError):
                pass
        else:
            await interaction.response.send_message(
                "この承認は期限切れ、解決済み、または許可された依頼者と一致しません。",
                ephemeral=True,
            )

    @discord.ui.button(label="承認（1回）", style=discord.ButtonStyle.danger, custom_id="remote_agent:approve")
    async def approve(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._resolve(interaction, True)

    @discord.ui.button(label="拒否", style=discord.ButtonStyle.secondary, custom_id="remote_agent:deny")
    async def deny(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._resolve(interaction, False)

    async def on_timeout(self) -> None:
        self.bridge.approvals.resolve(
            self.ticket.code,
            False,
            self.ticket.guild_id,
            self.ticket.channel_id,
            self.ticket.user_id,
        )
        self.bridge.audit.write(
            "approval_timeout",
            request_id=self.ticket.request_id,
            thread_id=self.ticket.thread_id,
            turn_id=self.ticket.turn_id,
            user_id=self.ticket.user_id,
            channel_id=self.ticket.channel_id,
            decision="denied",
        )


class BridgeBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!codex ", intents=intents, help_command=None)
        self.settings = settings
        scope = {
            "guild_id": str(settings.guild_id),
            "channel_id": str(settings.channel_id),
            "workdir": str(settings.workdir),
            "model": settings.model,
            "user_ids": ",".join(str(item) for item in sorted(settings.user_ids)),
        }
        self.sessions = SessionStore(settings.session_file, scope)
        self.audit = AuditLog(settings.audit_file)
        self.approvals = ApprovalManager()
        self.server = AppServer(
            settings.codex_bin,
            settings.codex_home,
            settings.workdir,
            self.on_app_event,
            self.on_server_request,
        )
        self.thread_id: str | None = self.sessions.load()
        self._thread_attached = False
        self.active_context: commands.Context[Any] | None = None
        self.active_turn_id: str | None = None
        self.active_turn_done: asyncio.Event | None = None
        self._turn_lock = asyncio.Lock()
        self._stopping = False
        self._file_items: dict[str, dict[str, Any]] = {}
        self._completed_turns: set[str] = set()
        self._log = logging.getLogger("remote_agent.bridge")

    async def setup_hook(self) -> None:
        await self.server.start()
        await self._register_commands()

    async def close(self) -> None:
        self.approvals.deny_all()
        await self.server.close()
        await super().close()

    async def on_disconnect(self) -> None:
        # An approval cannot outlive the Discord transport that presented it.
        self._stopping = True
        self.approvals.deny_all()
        if self.thread_id and self.active_turn_id:
            try:
                await self.server.rpc("turn/interrupt", {"threadId": self.thread_id, "turnId": self.active_turn_id}, timeout=5)
            except Exception:
                pass

    async def on_ready(self) -> None:
        self._log.info("Discord bridge connected as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id is not None or message.guild is None:
            return
        if message.guild.id != self.settings.guild_id or message.channel.id != self.settings.channel_id:
            return
        if not message.content.startswith("!codex "):
            return
        if message.author.id not in self.settings.user_ids:
            await message.channel.send("この操作は許可されていません。", allowed_mentions=discord.AllowedMentions.none())
            return
        await self.process_commands(message)

    async def _register_commands(self) -> None:
        @self.command(name="ask")
        async def ask(ctx: commands.Context[Any], *, prompt: str) -> None:
            await self.ask_codex(ctx, prompt)

        @self.command(name="status")
        async def status(ctx: commands.Context[Any]) -> None:
            if not await self.authorized(ctx):
                return
            busy = self._turn_lock.locked()
            state = "実行中" if busy else "待機中"
            thread = self.thread_id or "未作成"
            await ctx.reply(f"状態: {state}\n会話: {thread}", mention_author=False, allowed_mentions=discord.AllowedMentions.none())

        @self.command(name="new")
        async def new_thread(ctx: commands.Context[Any]) -> None:
            if not await self.authorized(ctx):
                return
            if self._turn_lock.locked():
                await self.reply(ctx, "実行中のため新しい会話を作れません。先に `!codex stop` を実行してください。")
                return
            async with self._turn_lock:
                try:
                    self.thread_id = await self._start_thread()
                    self._thread_attached = True
                    self.sessions.save(self.thread_id, has_turn=False)
                    await self.reply(ctx, f"新しい会話を開始しました: `{self.thread_id}`")
                except Exception as exc:
                    await self.reply(ctx, f"会話の開始に失敗しました: {self._safe_error(exc)}")

        @self.command(name="stop")
        async def stop(ctx: commands.Context[Any]) -> None:
            if not await self.authorized(ctx):
                return
            self._stopping = True
            self.approvals.deny_all()
            if self.thread_id and self.active_turn_id:
                try:
                    await self.server.rpc("turn/interrupt", {
                        "threadId": self.thread_id,
                        "turnId": self.active_turn_id,
                    })
                except Exception as exc:
                    await self.reply(ctx, f"停止要求に失敗しました: {self._safe_error(exc)}")
                    return
            await self.reply(ctx, "停止要求を送りました。承認待ちの操作は拒否しました。")

        @self.command(name="approve")
        async def approve(ctx: commands.Context[Any], code: str) -> None:
            await self.resolve_text_approval(ctx, code, True)

        @self.command(name="deny")
        async def deny(ctx: commands.Context[Any], code: str) -> None:
            await self.resolve_text_approval(ctx, code, False)

        @self.command(name="help")
        async def help_command(ctx: commands.Context[Any]) -> None:
            await self.reply(ctx, "`!codex ask <指示>` / `status` / `stop` / `new` / `approve <コード>` / `deny <コード>`")

    async def on_command_error(self, ctx: commands.Context[Any], error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await self.reply(ctx, "Usage: `!codex ask <instruction>`. Use `!codex help` for commands.")
        elif isinstance(error, commands.CommandNotFound):
            await self.reply(ctx, "Unknown command. Use `!codex help`.")
        else:
            self._log.warning("Discord command failed: %s", type(error).__name__)
            await self.reply(ctx, "Discord command failed.")

    async def authorized(self, ctx: commands.Context[Any]) -> bool:
        valid = (
            ctx.guild is not None
            and ctx.guild.id == self.settings.guild_id
            and ctx.channel.id == self.settings.channel_id
            and ctx.author.id in self.settings.user_ids
            and not ctx.author.bot
            and ctx.message.webhook_id is None
        )
        if not valid:
            await self.reply(ctx, "この操作は許可されていません。")
        return valid

    async def reply(self, ctx: commands.Context[Any], content: str) -> None:
        await ctx.reply(content, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def ask_codex(self, ctx: commands.Context[Any], prompt: str) -> None:
        if not await self.authorized(ctx):
            return
        prompt = prompt.strip()
        if not prompt:
            await self.reply(ctx, "指示を入力してください。")
            return
        if len(prompt) > self.settings.max_input_chars:
            await self.reply(ctx, f"指示が長すぎます（上限 {self.settings.max_input_chars} 文字）。")
            return
        if self._turn_lock.locked():
            await self.reply(ctx, "別の指示を処理中です。`!codex status` で状態を確認してください。")
            return

        async with self._turn_lock:
            self._stopping = False
            self.active_context = ctx
            self.active_turn_id = None
            done = asyncio.Event()
            self.active_turn_done = done
            try:
                if self.server._dead.is_set():
                    raise AppServerError("Codex App Server が切断されています")
                await self._ensure_thread()
                await self.reply(ctx, "Codex が指示を処理します。危険な操作はこのチャンネルで承認を求めます。")
                self.audit.write("turn_started", user_id=ctx.author.id, channel_id=ctx.channel.id, thread_id=self.thread_id)
                assert self.thread_id is not None
                self.sessions.save(self.thread_id, has_turn=True)
                result = await self.server.rpc("turn/start", {
                    "threadId": self.thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "cwd": str(self.settings.workdir),
                    "model": self.settings.model,
                    "approvalPolicy": "untrusted",
                    "approvalsReviewer": "user",
                    "sandboxPolicy": {
                        "type": "readOnly",
                        "access": {
                            "type": "restricted",
                            "readableRoots": [str(self.settings.workdir)],
                        },
                    },
                })
                turn = result.get("turn", {})
                self.active_turn_id = turn.get("id")
                if not self.active_turn_id:
                    raise AppServerError("Codex App Server did not return a turn ID")
                if self.active_turn_id in self._completed_turns:
                    done.set()
                if self._stopping:
                    await self.server.rpc("turn/interrupt", {"threadId": self.thread_id, "turnId": self.active_turn_id}, timeout=5)
                dead_wait = asyncio.create_task(self.server.wait_dead())
                done_wait = asyncio.create_task(done.wait())
                finished, pending = await asyncio.wait({dead_wait, done_wait}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if dead_wait in finished:
                    raise AppServerError("Codex App Server が処理中に切断されました")
            except asyncio.CancelledError:
                self.approvals.deny_all()
                raise
            except Exception as exc:
                self.approvals.deny_all()
                await self.reply(ctx, f"処理に失敗しました: {self._safe_error(exc)}")
                self.audit.write("turn_failed", user_id=ctx.author.id, channel_id=ctx.channel.id, reason=type(exc).__name__)
            finally:
                self.active_context = None
                self.active_turn_id = None
                self.active_turn_done = None
                self._stopping = False

    async def _start_thread(self) -> str:
        response = await self.server.rpc("thread/start", {
            "model": self.settings.model,
            "cwd": str(self.settings.workdir),
            "approvalPolicy": "untrusted",
            "approvalsReviewer": "user",
            "sandbox": "read-only",
        })
        thread = response.get("thread", {})
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise AppServerError("Codex App Server did not return a thread ID")
        self._verify_policy(response)
        return thread_id

    async def _ensure_thread(self) -> str:
        if self.thread_id:
            if self._thread_attached:
                return self.thread_id
            try:
                response = await self.server.rpc("thread/resume", {
                    "threadId": self.thread_id,
                    "model": self.settings.model,
                    "cwd": str(self.settings.workdir),
                    "approvalPolicy": "untrusted",
                    "approvalsReviewer": "user",
                    "sandbox": "read-only",
                })
                self._verify_policy(response)
                self._thread_attached = True
                return self.thread_id
            except Exception as exc:
                reason = str(exc).casefold()
                if not self.sessions.has_turn and "no rollout found" in reason:
                    self.sessions.clear()
                    self.thread_id = None
                else:
                    raise AppServerError("saved conversation could not be resumed; use `!codex new` after checking Codex status") from exc
        self.thread_id = await self._start_thread()
        self._thread_attached = True
        self.sessions.save(self.thread_id, has_turn=False)
        return self.thread_id

    @staticmethod
    def _verify_policy(response: dict[str, Any]) -> None:
        thread = response.get("thread") or {}
        policy = response.get("approvalPolicy") or thread.get("approvalPolicy")
        reviewer = response.get("approvalsReviewer") or thread.get("approvalsReviewer")
        sandbox = response.get("sandbox") or thread.get("sandbox") or {}
        sandbox_type = sandbox.get("type") if isinstance(sandbox, dict) else sandbox
        if policy != "untrusted" or reviewer != "user" or sandbox_type not in {"readOnly", "read-only"}:
            raise AppServerError("Codex App Server did not confirm untrusted approvals, user review, and read-only sandbox")

    async def resolve_text_approval(self, ctx: commands.Context[Any], code: str, approve: bool) -> None:
        if not await self.authorized(ctx):
            return
        if not code.isalnum() or len(code) != 6:
            await self.reply(ctx, "承認コードの形式が正しくありません。")
            return
        ticket = self.approvals.get(code)
        accepted = self.approvals.resolve(
            code,
            approve,
            ctx.guild.id if ctx.guild else 0,
            ctx.channel.id,
            ctx.author.id,
        )
        if accepted and ticket is not None:
            self.audit.write(
                "approval_decided",
                request_id=ticket.request_id,
                thread_id=ticket.thread_id,
                turn_id=ticket.turn_id,
                user_id=ctx.author.id,
                channel_id=ctx.channel.id,
                decision="approved_once" if approve else "denied",
            )
            await self.reply(ctx, "この操作を1回だけ承認しました。" if approve else "操作を拒否しました。")
        else:
            await self.reply(ctx, "承認コードが無効、期限切れ、または操作元と一致しません。")

    async def on_app_event(self, method: str, params: dict[str, Any]) -> None:
        event_thread = params.get("threadId")
        if event_thread and self.thread_id and event_thread != self.thread_id:
            return
        event_turn = params.get("turnId") or (params.get("turn") or {}).get("id")
        if event_turn and self.active_turn_id and event_turn != self.active_turn_id:
            return
        if method == "turn/started" and isinstance(event_turn, str) and self.active_turn_id is None:
            self.active_turn_id = event_turn
        if method == "turn/started":
            self._file_items.clear()
        if method == "item/started":
            item = params.get("item") or {}
            if item.get("type") in {"fileChange", "commandExecution"} and isinstance(item.get("id"), str):
                self._file_items[item["id"]] = item
                if len(self._file_items) > 200:
                    self._file_items.pop(next(iter(self._file_items)))
            if self.active_context is not None and item.get("type") in {"commandExecution", "fileChange"}:
                await self.reply(self.active_context, f"Codex: {item.get('type')} の処理を開始しました。")
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                text = item.get("text", "")
                if isinstance(text, str) and text.strip() and self.active_context is not None:
                    for chunk in _split_message(text):
                        await self.active_context.send(chunk, allowed_mentions=discord.AllowedMentions.none())
            if item.get("type") == "fileChange" and item.get("id"):
                self._file_items.pop(item["id"], None)
            if item.get("type") == "commandExecution" and item.get("id"):
                self._file_items.pop(item["id"], None)
        elif method == "item/agentMessage/delta":
            # Final assistant messages are sent from item/completed; deltas remain internal.
            return
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            turn_id = turn.get("id") or params.get("turnId")
            status = turn.get("status", "completed")
            if isinstance(turn_id, str):
                self._completed_turns.add(turn_id)
                if self.active_turn_done is not None and self.active_turn_id in (None, turn_id):
                    self.active_turn_done.set()
            if self.active_context is not None:
                await self.reply(self.active_context, f"Codex の処理が終了しました（{status}）。")

    async def on_server_request(self, message: dict[str, Any]) -> dict[str, Any]:
        method = message.get("method", "")
        params = message.get("params") or {}
        if method in {"__disconnect__", "__eventerror__"}:
            self.approvals.deny_all()
            if method == "__eventerror__" and self.thread_id and self.active_turn_id:
                try:
                    await self.server.rpc("turn/interrupt", {"threadId": self.thread_id, "turnId": self.active_turn_id}, timeout=5)
                except Exception:
                    pass
            return {}
        if method not in APPROVAL_METHODS:
            if method in {"mcpServer/elicitation/request", "item/permissions/requestApproval", "item/tool/requestUserInput"}:
                return self._deny_nonapproval(method, params)
            raise AppServerError("unsupported server request; refusing to grant access")
        if self.active_context is None or self.active_turn_id is None:
            # It is possible for the request to arrive before the turn/start response. The
            # item id is still bound by active context; verify thread and non-empty turn.
            if self.active_context is None:
                return self._approval_result(method, False)
        ctx = self.active_context
        if ctx is None or not self.context_is_live(ctx):
            return self._approval_result(method, False)
        thread_id = params.get("threadId") or params.get("conversationId")
        turn_id = params.get("turnId") or ""
        if not thread_id or thread_id != self.thread_id:
            return self._approval_result(method, False)
        if not turn_id or not self.active_turn_id or turn_id != self.active_turn_id:
            return self._approval_result(method, False)

        item_id = str(params.get("itemId") or params.get("callId") or "")
        if not item_id:
            return self._approval_result(method, False)
        if method.startswith("item/") and params.get("availableDecisions"):
            if "accept" not in params["availableDecisions"]:
                return self._approval_result(method, False)
        title, detail = self.approval_detail(method, params, item_id)
        if not item_id or not detail:
            self.audit.write("approval_denied", request_id=str(message.get("id")), thread_id=thread_id, turn_id=turn_id, user_id=ctx.author.id, channel_id=ctx.channel.id, decision="missing_context")
            return self._approval_result(method, False)
        if method in {"item/fileChange/requestApproval", "applyPatchApproval"} and params.get("grantRoot"):
            return self._approval_result(method, False)

        ticket = self.approvals.create(
            message.get("id"), method, item_id, str(thread_id or self.thread_id or ""), str(turn_id),
            ctx.guild.id if ctx.guild else 0, ctx.channel.id, ctx.author.id, title, detail,
            self.settings.approval_timeout,
        )
        if not await self.publish_approval(ctx, ticket):
            self.approvals.resolve(ticket.code, False, ticket.guild_id, ticket.channel_id, ticket.user_id)
            self._stopping = True
            try:
                await self.server.rpc("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=5)
            except Exception:
                pass
            return self._approval_result(method, False)

        approved = await self.approvals.wait(ticket, self.settings.approval_timeout)
        # A race with Discord disconnect, timeout, cancellation, or app-server shutdown
        # converts an approval to a denial before it reaches Codex.
        approved = (
            approved
            and self.context_is_live(ctx)
            and self.active_context is ctx
            and self.active_turn_id == ticket.turn_id
            and self.thread_id == ticket.thread_id
            and not self._stopping
            and not self.server._dead.is_set()
        )
        return self._approval_result(method, approved)

    def approval_detail(self, method: str, params: dict[str, Any], item_id: str) -> tuple[str, str]:
        if method in {"item/fileChange/requestApproval", "applyPatchApproval"}:
            item = self._file_items.get(item_id)
            if not item or not isinstance(item.get("changes"), list) or not item["changes"]:
                return "ファイル変更の承認", ""
            return "ファイル変更", _json({
                "item_id": item_id,
                "reason": params.get("reason"),
                "changes": item["changes"],
            })
        item = self._file_items.get(item_id) or {}
        command = params.get("command") or item.get("command")
        cwd = params.get("cwd") or item.get("cwd")
        if not command or not cwd:
            return "コマンド実行の承認", ""
        return "コマンド実行", _json({
            "item_id": item_id,
            "command": command,
            "cwd": cwd,
            "reason": params.get("reason"),
            "commandActions": params.get("commandActions") or item.get("commandActions"),
            "networkApprovalContext": params.get("networkApprovalContext"),
        })

    async def publish_approval(self, ctx: commands.Context[Any], ticket: ApprovalTicket) -> bool:
        body = (
            f"承認要求 `{ticket.code}`\n"
            f"種類: {ticket.title}\n"
            f"依頼者: <@{ticket.user_id}>\n"
            f"有効期限: {self.settings.approval_timeout} 秒\n"
            "ボタンまたは `!codex approve <コード>` / `!codex deny <コード>` で回答してください。\n"
            "承認はこの操作1回だけに適用されます。"
        )
        content: str | None = None
        attachment: discord.File | None = None
        if len(ticket.detail) <= 900:
            content = f"```json\n{ticket.detail}\n```"
        else:
            payload = ticket.detail.encode("utf-8")
            if len(payload) > 8 * 1024 * 1024:
                return False
            attachment = discord.File(io.BytesIO(payload), filename=f"approval-{ticket.code}.json")
            content = "操作内容全文は添付ファイルにあります。全文を確認してから回答してください。"
        try:
            await ctx.send(
                body + "\n" + (content or ""),
                file=attachment,
                view=ApprovalView(self, ticket),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.HTTPException, discord.Forbidden, OSError):
            return False
        self.audit.write(
            "approval_requested",
            request_id=ticket.request_id,
            thread_id=ticket.thread_id,
            turn_id=ticket.turn_id,
            user_id=ticket.user_id,
            channel_id=ticket.channel_id,
        )
        return True

    def _approval_result(self, method: str, approved: bool) -> dict[str, Any]:
        if method in {"execCommandApproval", "applyPatchApproval"}:
            decision: Any = "approved" if approved else "abort"
        else:
            decision = "accept" if approved else "decline"
        return {"decision": decision}

    def _deny_nonapproval(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method in {"mcpServer/elicitation/request"}:
            return {"action": "decline", "content": None}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        if method == "item/tool/requestUserInput":
            questions = params.get("questions") or []
            return {"answers": {q["id"]: {"answers": []} for q in questions if isinstance(q, dict) and isinstance(q.get("id"), str)}}
        raise AppServerError("unsupported server request")

    def context_is_live(self, ctx: commands.Context[Any]) -> bool:
        return (
            self.is_ready()
            and not self.is_closed()
            and ctx.guild is not None
            and ctx.guild.id == self.settings.guild_id
            and ctx.channel.id == self.settings.channel_id
            and ctx.author.id in self.settings.user_ids
        )

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        # Do not relay local environment values or exception payloads into Discord.
        if isinstance(exc, AppServerError):
            return str(exc)[:250]
        return f"{type(exc).__name__} (詳細はホストログを確認してください)"
