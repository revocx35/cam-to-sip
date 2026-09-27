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

    def bridges_for_phone(self, pid: str) -> list:
        return [b for b in self.config.bridges if b.phone_id == pid]

    def route_bridge(self, pid: str, caller: str):
        """Pick the bridge for an incoming call to phone `pid` from `caller`.

        A bridge whose allowed_callers lists the caller wins; otherwise the
        phone's default bridge (empty allowed_callers) takes the call.
        Returns (bridge, reason) or (None, reason).
        """
        bridges = [b for b in self.bridges_for_phone(pid) if b.enabled]
        if not bridges:
            return None, "no enabled bridge"
        caller = caller.strip()
        for b in bridges:
            if caller and caller in b.allowed_callers:
                return b, f"caller {caller} is listed"
        for b in bridges:
            if not b.allowed_callers:
                return b, "default bridge"
        return None, "caller not allowed"

    def routing_conflict(self, bridge) -> str | None:
        """Why `bridge` can't coexist with the phone's other enabled bridges (None if it can)."""
        if not bridge.enabled:
            return None
        for other in self.bridges_for_phone(bridge.phone_id):
            if other.id == bridge.id or not other.enabled:
                continue
            name = other.name or other.id
            if not bridge.allowed_callers and not other.allowed_callers:
                return (f"this phone already has a default bridge for all callers ('{name}'); "
                        "list allowed callers on one of them")
            overlap = sorted(set(bridge.allowed_callers) & set(other.allowed_callers))
            if overlap:
                return f"caller {', '.join(overlap)} is already routed to bridge '{name}'"
        return None
