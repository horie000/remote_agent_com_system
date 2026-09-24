from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    """Append small operational records without prompts, tokens, or file contents."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._log = logging.getLogger("remote_agent.audit")

    def write(self, event: str, **fields: Any) -> None:
        safe = {key: value for key, value in fields.items() if key in {
            "request_id", "thread_id", "turn_id", "user_id", "channel_id", "decision", "reason"
        }}
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **safe,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            self._log.exception("could not write audit record (%s)", event)
