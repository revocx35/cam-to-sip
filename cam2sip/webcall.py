"""Browser calls: talk to a camera straight from the web UI over a WebSocket.

Wire protocol on ``/api/cameras/{id}/talk`` (after the WebSocket is accepted):

* binary, server -> browser: camera microphone, G.711 A-law, 8 kHz mono, any length
* binary, browser -> server: browser microphone, G.711 A-law, 8 kHz mono (20 ms frames)
* text JSON, server -> browser: ``{"type": "status", ...}`` every second,
  ``{"type": "error", "message": ...}`` before closing on errors
* text JSON, browser -> server: ``{"type": "gate", "db": -55 | null}`` (noise gate for
  open-mic mode, null while push-to-talk), ``{"type": "hangup"}``
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from typing import TYPE_CHECKING

from starlette.websockets import WebSocket, WebSocketDisconnect

from .media import g711
from .models import Camera

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger("cam2sip.webcall")

WEB_CODEC = "PCMA"
DEFAULT_GATE_DB = -55.0
MAX_WEB_CALL_SECONDS = 3600
MAX_QUEUED_CHUNKS = 40          # ~5 s of camera audio; older audio is dropped


class WebCall:
    def __init__(self, engine: "Engine", camera: Camera, ws: WebSocket, client: str):
        from .engine import CameraLink  # local import: engine imports this module

        self.engine = engine
        self.camera = camera
        self.ws = ws
        self.client = client
        self.id = secrets.token_hex(4)
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.end_reason = ""
        self.gate_db: float | None = DEFAULT_GATE_DB
        self.link = CameraLink(engine, camera, self._on_mic)
        self.rx_frames = 0
        self.tx_frames = 0
        self._out: asyncio.Queue[bytes] = asyncio.Queue()
        self._done = asyncio.Event()

    # -- camera -> browser ---------------------------------------------------------------
    def _on_mic(self, codec: str, payload: bytes) -> None:
        if self._out.qsize() >= MAX_QUEUED_CHUNKS:   # slow client: keep latency bounded
            with contextlib.suppress(asyncio.QueueEmpty):
                self._out.get_nowait()
        self._out.put_nowait(g711.convert(payload, codec, WEB_CODEC))

    async def _sender(self) -> None:
        while True:
            data = await self._out.get()
            await self.ws.send_bytes(data)
            self.tx_frames += 1

    async def _status(self) -> None:
        while True:
            duration = time.time() - self.started_at
            if duration > MAX_WEB_CALL_SECONDS:
                await self.hangup("max call duration reached")
                return
            await self.ws.send_text(json.dumps(dict(self.link.info(), type="status", id=self.id,
                                                    duration=int(duration))))
            await asyncio.sleep(1)

    # -- lifecycle --------------------------------------------------------------------------
    async def run(self) -> None:
        """Serve the call until the browser disconnects or it is hung up."""
        log.info("web call %s to %s started from %s", self.id, self.camera.name, self.client)
        self.link.start()
        self.link.start_notice()          # privacy notice (if enabled) before audio is shared
        tasks = [asyncio.create_task(self._sender()), asyncio.create_task(self._status()),
                 asyncio.create_task(self._receiver()), asyncio.create_task(self._done.wait())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = None if t.cancelled() else t.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    log.debug("web call %s: %r", self.id, exc)
        finally:
            for t in tasks:
                t.cancel()
            await self.link.stop()
            self.ended_at = time.time()
            self.end_reason = self.end_reason or "browser hung up"
            with contextlib.suppress(Exception):
                await self.ws.close()
            log.info("web call %s ended: %s", self.id, self.end_reason)

    async def _receiver(self) -> None:
        while True:
            msg = await self.ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data:
                self.rx_frames += 1
                self.link.speak(data, WEB_CODEC, 0.0, self.gate_db)
                continue
            text = msg.get("text")
            if not text:
                continue
            try:
                cmd = json.loads(text)
            except ValueError:
                continue
            if cmd.get("type") == "gate":
                db = cmd.get("db")
                self.gate_db = float(db) if isinstance(db, (int, float)) else None
            elif cmd.get("type") == "hangup":
                self.end_reason = "browser hung up"
                return

    async def hangup(self, reason: str) -> None:
        self.end_reason = reason
        with contextlib.suppress(Exception):
            await self.ws.send_text(json.dumps({"type": "ended", "reason": reason}))
        self._done.set()

    def info(self) -> dict:
        """Same shape as a SIP call's info() so the dashboard can show both."""
        return {
            "id": self.id,
            "direction": "web",
            "state": "active",
            "remote": "Web UI",
            "remote_display": f"Web call ({self.client})",
            "codec": WEB_CODEC,
            "created_at": self.started_at,
            "answered_at": self.started_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason,
            "rtp": {"local_port": None, "remote": self.client,
                    "rx_packets": self.rx_frames, "tx_packets": self.tx_frames},
            "media": self.link.info(),
        }
