from __future__ import annotations

import json
from pathlib import Path


class SessionStore:
    def __init__(self, path: Path, scope: dict[str, str]) -> None:
        self.path = path
        self.scope = scope
        self.has_turn = True

    def load(self) -> str | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            value = data.get("thread_id")
            if isinstance(value, str) and value and data.get("scope") == self.scope:
                self.has_turn = bool(data.get("has_turn", True))
                return value
            return None
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, AttributeError):
            return None

    def save(self, thread_id: str, has_turn: bool = True) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"thread_id": thread_id, "scope": self.scope, "has_turn": has_turn}), encoding="utf-8")
        temp.replace(self.path)
        self.has_turn = has_turn

    def clear(self) -> None:
        self.has_turn = False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
