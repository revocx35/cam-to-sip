"""JSON file persistence for the configuration and call history."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from .models import Config

log = logging.getLogger("cam2sip.store")

HISTORY_LIMIT = 200


class Store:
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "config.json"
        self.history_path = self.dir / "call_history.json"
        self._lock = threading.Lock()
        self.config = self._load()
        self.history: list[dict] = self._load_history()

    def _load(self) -> Config:
        if self.path.exists():
            try:
                return Config.model_validate_json(self.path.read_text())
            except Exception as e:
                backup = self.path.with_suffix(".json.broken")
                self.path.replace(backup)
                log.error("config.json unreadable (%s) - moved to %s, starting fresh", e, backup)
        cfg = Config()
        self._write(self.path, cfg.model_dump_json(indent=2))
        return cfg

    def _load_history(self) -> list[dict]:
        try:
            return json.loads(self.history_path.read_text())[-HISTORY_LIMIT:]
        except Exception:
            return []

    def _write(self, path: Path, text: str) -> None:
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except Exception:
                os.unlink(tmp)
                raise

    def save(self) -> None:
        self._write(self.path, self.config.model_dump_json(indent=2))

    def add_history(self, entry: dict) -> None:
        self.history.append(entry)
        self.history = self.history[-HISTORY_LIMIT:]
        self._write(self.history_path, json.dumps(self.history))

    # -- lookups ---------------------------------------------------------------
    def camera(self, cid: str):
        return next((c for c in self.config.cameras if c.id == cid), None)

    def phone(self, pid: str):
        return next((p for p in self.config.phones if p.id == pid), None)

    def bridge(self, bid: str):
        return next((b for b in self.config.bridges if b.id == bid), None)

    def bridge_for_phone(self, pid: str):
        return next((b for b in self.config.bridges if b.phone_id == pid), None)
