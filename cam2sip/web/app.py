"""FastAPI application: REST API + static single-page web UI."""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .. import onvif
from ..engine import CameraBusy, Engine
from ..logbuffer import LogBuffer
from ..models import SECRET_FIELDS, Bridge, Camera, Phone, redact
from ..settings import Settings
from ..store import Store
from . import auth

log = logging.getLogger("cam2sip.web")
STATIC = Path(__file__).parent / "static"
PUBLIC = {"/api/health", "/api/session", "/api/login", "/api/logout", "/api/setup"}


class PasswordBody(BaseModel):
    password: str


class ChangePasswordBody(BaseModel):
    current: str
    new: str


class DialBody(BaseModel):
    target: str


class DiscoverBody(BaseModel):
    host: str
    port: int = 2020
    username: str = ""
    password: str = ""
    id: str | None = None


def setup_logging(settings: Settings, buffer: LogBuffer) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, LogBuffer) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    buffer.setLevel(logging.INFO)
    root.addHandler(buffer)
    logging.getLogger("cam2sip").setLevel(settings.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    if settings.sip_trace:
        logging.getLogger("cam2sip.sip.trace").setLevel(logging.DEBUG)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logbuf = LogBuffer()
    setup_logging(settings, logbuf)
    store = Store(settings.data_dir)
    engine = Engine(settings, store)
    cfg = store.config

    if settings.admin_password and not cfg.settings.admin_password_hash:
        cfg.settings.admin_password_hash = auth.hash_password(settings.admin_password)
        store.save()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("cam2sip %s starting (web :%d, sip udp :%d, rtp %d-%d)", settings.version,
                 settings.web_port, settings.sip_port, settings.rtp_port_min, settings.rtp_port_max)
        await engine.start()
        yield
        await engine.stop()

    app = FastAPI(title="cam2sip", version=settings.version, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.engine = engine
    app.state.store = store
    app.state.logs = logbuf

    # -- auth -------------------------------------------------------------------------
    def authed(request: Request) -> bool:
        s = store.config.settings
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer ") and s.api_token:
            return hmac.compare_digest(header[7:].strip(), s.api_token)
        return auth.check_session(request.cookies.get(auth.COOKIE), s.session_secret, s.admin_password_hash)

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in PUBLIC and not path.startswith(("/api/docs", "/api/openapi")):
            if not authed(request):
                return JSONResponse({"detail": "not authenticated"}, status_code=401)
        return await call_next(request)

    def set_cookie(resp: Response) -> None:
        s = store.config.settings
        resp.set_cookie(auth.COOKIE, auth.make_session(s.session_secret, s.admin_password_hash),
                        max_age=auth.SESSION_TTL, httponly=True, samesite="strict",
                        secure=settings.secure_cookies)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": settings.version}

    @app.get("/api/session")
    async def session(request: Request):
        return {"authenticated": authed(request),
                "setup_required": not store.config.settings.admin_password_hash,
                "version": settings.version}

    @app.post("/api/setup")
    async def setup(body: PasswordBody, response: Response):
        if store.config.settings.admin_password_hash:
            raise HTTPException(409, "already set up")
        if len(body.password) < 6:
            raise HTTPException(400, "password must be at least 6 characters")
        store.config.settings.admin_password_hash = auth.hash_password(body.password)
        store.save()
        set_cookie(response)
        return {"ok": True}

    @app.post("/api/login")
    async def login(body: PasswordBody, response: Response):
        h = store.config.settings.admin_password_hash
        if not h or not auth.verify_password(body.password, h):
            await asyncio.sleep(1.0)
            raise HTTPException(401, "wrong password")
        set_cookie(response)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(auth.COOKIE)
        return {"ok": True}

    # -- helpers ------------------------------------------------------------------------
    def merge(model_cls, existing, payload: dict):
        data = existing.model_dump() if existing else {}
        for k, v in payload.items():
            if k in ("id", "contact_user") and existing:
                continue
            if k in SECRET_FIELDS and (v is None or v == "") and existing:
                continue
            if k.endswith("_set"):
                continue
            data[k] = v
        try:
            return model_cls.model_validate(data)
        except ValidationError as e:
            first = e.errors()[0]
            field = ".".join(str(x) for x in first.get("loc", []))
            raise HTTPException(422, f"{field}: {first.get('msg')}") from e

    async def save_and_apply():
        store.save()
        await engine.apply_config()

    def find(items, oid):
        item = next((x for x in items if x.id == oid), None)
        if not item:
            raise HTTPException(404, "not found")
        return item

    # -- status / logs --------------------------------------------------------------------
    @app.get("/api/status")
    async def status():
        st = engine.status()
        st["counts"] = {"cameras": len(cfg.cameras), "phones": len(cfg.phones), "bridges": len(cfg.bridges)}
        return st

    @app.get("/api/logs")
    async def logs(after: int = 0):
        return {"seq": logbuf.seq, "records": logbuf.since(after)}

    # -- cameras ----------------------------------------------------------------------------
    @app.get("/api/cameras")
    async def list_cameras():
        return [dict(redact(c), busy=c.id in engine.busy, probe=engine.probes.get(c.id)) for c in cfg.cameras]

    @app.post("/api/cameras")
    async def create_camera(payload: dict):
        cam = merge(Camera, None, {k: v for k, v in payload.items() if k != "id"})
        cfg.cameras.append(cam)
        await save_and_apply()
        log.info("camera '%s' added", cam.name)
        engine.probe_later(cam)
        return redact(cam)

    @app.put("/api/cameras/{cid}")
    async def update_camera(cid: str, payload: dict):
        old = find(cfg.cameras, cid)
        cam = merge(Camera, old, payload)
        cfg.cameras[cfg.cameras.index(old)] = cam
        engine.probes.pop(cid, None)
        await save_and_apply()
        engine.probe_later(cam)
        return redact(cam)

    @app.delete("/api/cameras/{cid}")
    async def delete_camera(cid: str):
        cam = find(cfg.cameras, cid)
        used = [b.name or b.id for b in cfg.bridges if b.camera_id == cid]
        if used:
            raise HTTPException(409, f"camera is used by bridge(s): {', '.join(used)}")
        cfg.cameras.remove(cam)
        engine.probes.pop(cid, None)
        await save_and_apply()
        return {"ok": True}

    @app.post("/api/cameras/probe")
    async def probe_camera(payload: dict):
        existing = store.camera(payload.get("id") or "")
        cam = merge(Camera, existing, payload)
        try:
            return await engine.probe_camera(cam)
        except CameraBusy as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/cameras/onvif-discover")
    async def onvif_discover(body: DiscoverBody):
        password = body.password
        if not password and body.id and store.camera(body.id):
            password = store.camera(body.id).password
        try:
            return await onvif.discover(body.host, body.port, body.username, password)
        except onvif.OnvifError as e:
            raise HTTPException(502, str(e)) from e

    @app.get("/api/cameras/{cid}/snapshot.jpg")
    async def snapshot(cid: str):
        cam = find(cfg.cameras, cid)
        try:
            data = await engine.snapshot(cam)
        except Exception as e:
            raise HTTPException(502, f"snapshot failed: {e}") from e
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/cameras/{cid}/test-speaker")
    async def test_speaker(cid: str):
        cam = find(cfg.cameras, cid)
        try:
            return await engine.test_speaker(cam)
        except CameraBusy as e:
            raise HTTPException(409, str(e)) from e
        except Exception as e:
            raise HTTPException(502, f"speaker test failed: {e or 'timeout'}") from e

    @app.get("/api/cameras/{cid}/mic.wav")
    async def mic_wav(cid: str, seconds: float = 4.0):
        cam = find(cfg.cameras, cid)
        try:
            data = await engine.record_mic(cam, max(1.0, min(seconds, 15.0)))
        except Exception as e:
            raise HTTPException(502, f"microphone test failed: {e}") from e
        return Response(data, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    # -- phones -------------------------------------------------------------------------------
    @app.get("/api/phones")
    async def list_phones():
        st = engine.status()["phones"]
        return [dict(redact(p), status=st.get(p.id, {"state": "disabled"})) for p in cfg.phones]

    @app.post("/api/phones")
    async def create_phone(payload: dict):
        phone = merge(Phone, None, {k: v for k, v in payload.items() if k not in ("id", "contact_user")})
        cfg.phones.append(phone)
        await save_and_apply()
        log.info("virtual phone '%s' (%s@%s) added", phone.name, phone.username, phone.server)
        return redact(phone)

    @app.put("/api/phones/{pid}")
    async def update_phone(pid: str, payload: dict):
        old = find(cfg.phones, pid)
        phone = merge(Phone, old, payload)
        cfg.phones[cfg.phones.index(old)] = phone
        await save_and_apply()
        return redact(phone)

    @app.delete("/api/phones/{pid}")
    async def delete_phone(pid: str):
        phone = find(cfg.phones, pid)
        used = [b.name or b.id for b in cfg.bridges if b.phone_id == pid]
        if used:
            raise HTTPException(409, f"phone is used by bridge(s): {', '.join(used)}")
        cfg.phones.remove(phone)
        await save_and_apply()
        return {"ok": True}

    @app.post("/api/phones/{pid}/register")
    async def reregister(pid: str):
        find(cfg.phones, pid)
        acc = engine.ua.accounts.get(pid)
        if acc:
            acc.refresh_now()
        return {"ok": True}

    # -- bridges --------------------------------------------------------------------------------
    def check_bridge(b: Bridge, exclude: str | None = None) -> None:
        if not store.camera(b.camera_id):
            raise HTTPException(422, "camera_id: unknown camera")
        if not store.phone(b.phone_id):
            raise HTTPException(422, "phone_id: unknown phone")
        other = next((x for x in cfg.bridges if x.phone_id == b.phone_id and x.id != exclude), None)
        if other:
            raise HTTPException(409, f"that phone is already bridged ('{other.name or other.id}')")

    @app.get("/api/bridges")
    async def list_bridges():
        return [dict(b.model_dump(), busy=b.camera_id in engine.busy) for b in cfg.bridges]

    @app.post("/api/bridges")
    async def create_bridge(payload: dict):
        b = merge(Bridge, None, {k: v for k, v in payload.items() if k != "id"})
        check_bridge(b)
        cfg.bridges.append(b)
        await save_and_apply()
        log.info("bridge '%s' created", b.name or b.id)
        return b.model_dump()

    @app.put("/api/bridges/{bid}")
    async def update_bridge(bid: str, payload: dict):
        old = find(cfg.bridges, bid)
        b = merge(Bridge, old, payload)
        check_bridge(b, exclude=bid)
        cfg.bridges[cfg.bridges.index(old)] = b
        await save_and_apply()
        return b.model_dump()

    @app.delete("/api/bridges/{bid}")
    async def delete_bridge(bid: str):
        b = find(cfg.bridges, bid)
        cfg.bridges.remove(b)
        await save_and_apply()
        return {"ok": True}

    @app.post("/api/bridges/{bid}/call")
    async def bridge_call(bid: str, body: DialBody):
        find(cfg.bridges, bid)
        target = body.target.strip()
        if not target:
            raise HTTPException(422, "target is required")
        try:
            call = await engine.dial(bid, target)
        except CameraBusy as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"call_id": call.id}

    # -- calls ----------------------------------------------------------------------------------
    @app.get("/api/calls")
    async def calls():
        return {"active": engine.status()["calls"], "history": list(reversed(store.history))}

    @app.post("/api/calls/{call_id}/hangup")
    async def hangup(call_id: str):
        if not await engine.hangup(call_id):
            raise HTTPException(404, "no such active call")
        return {"ok": True}

    # -- settings -------------------------------------------------------------------------------
    @app.get("/api/settings")
    async def get_settings():
        return {
            "version": settings.version,
            "api_token": cfg.settings.api_token,
            "sip_port": settings.sip_port,
            "rtp_ports": f"{settings.rtp_port_min}-{settings.rtp_port_max}",
            "advertise_ip": settings.advertise_ip or "auto",
            "web_port": settings.web_port,
            "go2rtc_api": settings.go2rtc_api,
            "data_dir": settings.data_dir,
        }

    @app.post("/api/settings/password")
    async def change_password(body: ChangePasswordBody, response: Response):
        s = cfg.settings
        if not auth.verify_password(body.current, s.admin_password_hash):
            raise HTTPException(401, "current password is wrong")
        if len(body.new) < 6:
            raise HTTPException(400, "password must be at least 6 characters")
        s.admin_password_hash = auth.hash_password(body.new)
        store.save()
        set_cookie(response)
        return {"ok": True}

    @app.post("/api/settings/api-token")
    async def regenerate_token():
        cfg.settings.api_token = secrets.token_urlsafe(24)
        store.save()
        return {"api_token": cfg.settings.api_token}

    # -- UI -------------------------------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
