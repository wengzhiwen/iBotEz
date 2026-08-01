"""Persistent bridge state: poll watermark + chat -> Pi session file mapping."""
from __future__ import annotations

import json
from pathlib import Path


class SessionMap:
    def __init__(
        self,
        watermark: int = 0,
        sessions: dict[str, str] | None = None,
        path: Path | None = None,
    ):
        self.watermark = watermark
        self.sessions = sessions or {}
        self.path = path

    @classmethod
    def load(cls, path: Path) -> "SessionMap":
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                int(data.get("watermark", 0)),
                {str(k): str(v) for k, v in dict(data.get("sessions", {})).items()},
                path,
            )
        return cls(path=path)

    def save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"watermark": self.watermark, "sessions": self.sessions}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)
