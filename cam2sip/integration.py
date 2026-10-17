"""Integration API v1 for telerouter (and similar callers).

  GET  /api/integration/v1/info      app, version, SIP port
  GET  /api/integration/v1/targets   the cameras that can be called
  POST /api/integration/v1/calls     {"target": camera id}
                                     -> {"sip_uri": "sip:<one-time user>@<ip>:<port>", "expires_in": 30}

Auth: `Authorization: Bearer <integration token>` (Settings -> Integrations; shown once, only its SHA-256
is stored; it reaches nothing but these endpoints). HTTPS only.

A call: the caller asks for a ticket, then sends a SIP INVITE to the one-time address from the same IP
within 30 s. cam2sip answers it like a direct bridge to that camera (privacy notice included).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .engine import Engine
    from .store import Store

log = logging.getLogger("cam2sip.integration")

TICKET_TTL = 30.0
MAX_TICKETS = 20


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "c2s_" + secrets.token_urlsafe(32)


@dataclass
class Ticket:
    user: str
    camera_id: str
    source_ip: str
    expires: float = field(default_factory=lambda: time.monotonic() + TICKET_TTL)


class Tickets:
    """One-time SIP addresses for direct calls (single use, short-lived, bound to the caller's IP)."""

    def __init__(self) -> None:
        self._t: dict[str, Ticket] = {}

    def create(self, camera_id: str, source_ip: str) -> Ticket:
        now = time.monotonic()
        self._t = {k: v for k, v in self._t.items() if v.expires > now}
        if len(self._t) >= MAX_TICKETS:
            raise HTTPException(429, "too many pending calls")
        t = Ticket("tr" + secrets.token_hex(16), camera_id, source_ip)
        self._t[t.user] = t
        return t

    def peek(self, user: str) -> Ticket | None:
        t = self._t.get(user)
        return t if t and t.expires >= time.monotonic() else None

    def take(self, user: str, source_ip: str) -> Ticket | None:
        t = self.peek(user)
        if not t:
            return None
        if t.source_ip and source_ip and t.source_ip != source_ip:
            log.warning("integration call ticket used from %s instead of %s - refused", source_ip, t.source_ip)
            return None
        del self._t[user]
        return t


class CallBody(BaseModel):
    target: str = Field(max_length=64)


def router(engine: "Engine", store: "Store") -> APIRouter:
    r = APIRouter(prefix="/api/integration/v1")

    def check(request: Request) -> str:
        if request.url.scheme != "https":
            raise HTTPException(403, "the integration API is served over HTTPS only (CAM2SIP_HTTPS_PORT)")
        s = store.config.settings
        h = request.headers.get("authorization", "")
        tok = h[7:].strip() if h[:7].lower() == "bearer " else ""
        if not s.integration_token_hash or not tok or \
                not hmac.compare_digest(token_hash(tok), s.integration_token_hash):
            raise HTTPException(401, "invalid integration token")
        return request.client.host if request.client else ""

    @r.get("/info")
    async def info(request: Request):
        check(request)
        return {"app": "cam2sip", "version": engine.settings.version, "sip_port": engine.settings.sip_port}

    @r.get("/targets")
    async def targets(request: Request):
        check(request)
        return {"targets": [{"id": c.id, "name": c.name, "kind": "camera", "busy": c.id in engine.busy}
                            for c in store.config.cameras if c.enabled]}

    @r.post("/calls")
    async def call(body: CallBody, request: Request):
        ip = check(request)
        cam = store.camera(body.target)
        if not cam or not cam.enabled:
            raise HTTPException(404, "unknown camera")
        if cam.id in engine.busy:
            raise HTTPException(409, f"camera {cam.name} is busy")
        t = engine.tickets.create(cam.id, ip)
        host = engine.settings.advertise_ip or _local_ip_for(ip)
        log.info("integration call ticket for camera %s (from %s)", cam.name, ip)
        return {"sip_uri": f"sip:{t.user}@{host}:{engine.settings.sip_port}", "expires_in": int(TICKET_TTL)}

    return r


def _local_ip_for(peer: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer or "192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
