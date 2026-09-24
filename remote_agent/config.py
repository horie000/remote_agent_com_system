from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _required_int(name: str) -> int:
    value = os.getenv(name, "").strip()
    if not value.isdecimal():
        raise ValueError(f"{name} must be a Discord numeric ID")
    return int(value)


@dataclass(frozen=True)
class Settings:
    discord_token: str
    guild_id: int
    channel_id: int
    user_ids: frozenset[int]
    workdir: Path
    state_dir: Path
    codex_home: Path
    codex_bin: str
    model: str
    approval_timeout: int
    max_input_chars: int

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN is required")
        users: set[int] = set()
        for item in os.getenv("DISCORD_ALLOWED_USER_IDS", "").split(","):
            item = item.strip()
            if item:
                if not item.isdecimal():
                    raise ValueError("DISCORD_ALLOWED_USER_IDS must contain numeric IDs")
                users.add(int(item))
        if not users:
            raise ValueError("DISCORD_ALLOWED_USER_IDS must contain at least one ID")

        state_dir = Path(os.getenv("BRIDGE_STATE_DIR", ".state")).expanduser().resolve()
        # Never inherit a normal user's Codex profile. It may contain MCP servers or hooks.
        home_value = os.getenv("BRIDGE_CODEX_HOME", "").strip()
        codex_home = Path(home_value).expanduser().resolve() if home_value else Path.home() / ".remote_agent_com_sys" / "codex-home"
        timeout = int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "120"))
        max_input = int(os.getenv("MAX_INPUT_CHARS", "10000"))
        if timeout < 10 or timeout > 3600:
            raise ValueError("APPROVAL_TIMEOUT_SECONDS must be from 10 to 3600")
        if max_input < 1 or max_input > 50000:
            raise ValueError("MAX_INPUT_CHARS must be from 1 to 50000")

        workdir = Path(os.getenv("CODEX_WORKDIR", ".")).expanduser().resolve()
        if not workdir.is_dir():
            raise ValueError("CODEX_WORKDIR must be an existing directory")
        if codex_home == workdir or workdir in codex_home.parents:
            raise ValueError("BRIDGE_CODEX_HOME must be outside CODEX_WORKDIR")

        return cls(
            discord_token=token,
            guild_id=_required_int("DISCORD_GUILD_ID"),
            channel_id=_required_int("DISCORD_CHANNEL_ID"),
            user_ids=frozenset(users),
            workdir=workdir,
            state_dir=state_dir,
            codex_home=codex_home,
            codex_bin=os.getenv("CODEX_BIN", "codex").strip() or "codex",
            model=os.getenv("CODEX_MODEL", "gpt-6-luna").strip() or "gpt-6-luna",
            approval_timeout=timeout,
            max_input_chars=max_input,
        )

    @property
    def session_file(self) -> Path:
        return self.state_dir / "session.json"

    @property
    def audit_file(self) -> Path:
        return self.state_dir / "audit.jsonl"
