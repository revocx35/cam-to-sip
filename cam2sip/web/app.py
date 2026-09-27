"""FastAPI application: REST API + static single-page web UI."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import math
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
import websockets
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .. import onvif
from ..media import piper as piper_mod
from ..media import tts as tts_mod
from ..engine import CameraBusy, Engine
from ..logbuffer import LogBuffer
from ..models import SECRET_FIELDS, Bridge, Camera, IvrOption, Phone, redact
from ..sounds import MAX_UPLOAD_BYTES, to_wav
from ..settings import Settings
from ..store import Store
from . import auth
from .tls import ensure_certificate

log = logging.getLogger("cam2sip.web")
STATIC = Path(__file__).parent / "static"
PUBLIC = {"/api/health", "/api/session", "/api/login", "/api/logout", "/api/setup"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def cross_site(headers, method: str = "POST") -> bool:
    """True for a browser request that another site started (Fetch Metadata).

    SameSite=strict keeps the cookie off cross-site requests, but not off requests from sibling
    subdomains (same site), and it doesn't cover /api/setup, which needs no cookie. Only our own
    pages may change state or open WebSockets; plain navigations are fine. Clients that don't send
    Sec-Fetch-Site (curl, Home Assistant) aren't browsers and are unaffected.
    """
    site = headers.get("sec-fetch-site")
    if site is None or site in ("same-origin", "none"):
        return False
    return not (method in SAFE_METHODS and headers.get("sec-fetch-mode") == "navigate")


def foreign_origin(headers) -> bool:
    """True when the Origin header names another host than the one the request was sent to.

    WebSockets aren't covered by CORS, so the handshake's Origin is what tells which page opened
    them, and every browser sends it (RFC 6455), including the ones without Sec-Fetch-Site.
    Clients without Origin (automations, tests) aren't browsers and pass. The hostname must match
    the Host header, and the port too when Host carries one: proxies may drop it (nginx `$host`).
    """
    origin = headers.get("origin")
    if origin is None:
        return False
    try:
        o = urlsplit(origin)
        h = urlsplit("//" + headers.get("host", ""))
        origin_port = o.port or {"http": 80, "https": 443}.get(o.scheme)
        host_port = h.port
    except ValueError:
        return True
    if not o.hostname or o.hostname != h.hostname:     # also "null" (sandboxed frames, file://)
        return True
    return host_port is not None and host_port != origin_port


class PasswordBody(BaseModel):
    password: str = Field(max_length=1024)


class ChangePasswordBody(BaseModel):
    current: str = Field(max_length=1024)
    new: str = Field(max_length=1024)


class DialBody(BaseModel):
    target: str
    camera_id: str | None = None


class IvrPreviewBody(BaseModel):
    ivr_options: list[IvrOption] = []
    ivr_greeting: str = ""
    ivr_greeting_sound: str = ""
    ivr_option_text: str = "Press {digit} for {name}."
    ivr_voice: str = "en-us"
    ivr_speed: int = 150
    text: str | None = None       # a single prompt instead of the composed menu
    sound: str = ""               # ... or an uploaded sound


class NoticeBody(BaseModel):
    notify_text: str | None = None
    notify_sound: str | None = None
    notify_voice: str | None = None
    notify_speed: int | None = None


class RenameBody(BaseModel):
    name: str


class DiscoverBody(BaseModel):
    host: str
    port: int = 2020
    username: str = ""
    password: str = ""
    id: str | None = None


class _QuietWebSocketLogs(logging.Filter):
    """Drop uvicorn's per-WebSocket 'connection open/closed/accepted' lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return msg not in ("connection open", "connection closed") and ' - "WebSocket ' not in msg


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
    logging.getLogger("uvicorn.error").addFilter(_QuietWebSocketLogs())
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
        if len(settings.admin_password) < auth.MIN_PASSWORD_LENGTH:
            log.warning("ADMIN_PASSWORD is shorter than %d characters - choose a longer one before "
                        "exposing the UI", auth.MIN_PASSWORD_LENGTH)
        cfg.settings.admin_password_hash = auth.hash_password(settings.admin_password)
        store.save()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("cam2sip %s starting (web :%d, sip udp :%d, rtp %d-%d)", settings.version,
                 settings.web_port, settings.sip_port, settings.rtp_port_min, settings.rtp_port_max)
        await engine.start()
        https = await start_https(_app)
        yield
        if https:
            https[0].should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(https[1], 5)
        await engine.stop()

    async def start_https(asgi_app):
        """Second listener with TLS (same app), so browsers allow the microphone."""
        if not settings.https_port:
            return None
        pair = ensure_certificate(settings.data_dir, settings.tls_cert, settings.tls_key)
        if not pair:
            return None
        config = uvicorn.Config(asgi_app, host=settings.web_host, port=settings.https_port,
                                ssl_certfile=pair[0], ssl_keyfile=pair[1], lifespan="off",
                                log_config=None, access_log=False, proxy_headers=False,
                                timeout_graceful_shutdown=3)
        server = _NoSignalServer(config)
        task = asyncio.create_task(server.serve())
        for _ in range(50):
            if server.started or task.done():
                break
            await asyncio.sleep(0.05)
        if task.done():
            log.warning("HTTPS listener on :%d failed to start", settings.https_port)
            return None
        log.info("HTTPS (self-signed unless CAM2SIP_TLS_CERT is set) on port %d", settings.https_port)
        return server, task

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
        if path.startswith("/api/"):
            if cross_site(request.headers, request.method):
                return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
            if path not in PUBLIC and not authed(request):
                return JSONResponse({"detail": "not authenticated"}, status_code=401)
        return await call_next(request)

    # X-Forwarded-For/-Proto count only from trusted reverse proxies (CAM2SIP_TRUSTED_PROXIES).
    # Added last, so it runs first; uvicorn's own proxy handling is off (see __main__/start_https).
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxies)

    throttle = auth.LoginThrottle()
    scrypt_slots = asyncio.Semaphore(2)

    async def password_ok(password: str, stored: str) -> bool:
        # scrypt takes ~50 ms of CPU: run it off the event loop so calls keep their audio
        async with scrypt_slots:
            return await asyncio.to_thread(auth.verify_password, password, stored)

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def charge_attempt(ip: str) -> None:
        try:
            throttle.begin(ip)
        except auth.Locked as e:
            wait = max(1, math.ceil(e.retry_after))
            hint = f"{wait} s" if wait < 120 else f"{math.ceil(wait / 60)} min"
            raise HTTPException(429, f"too many wrong passwords - try again in {hint}",
                                headers={"Retry-After": str(wait)}) from None

    def cookie_secure(request: Request) -> bool:
        if settings.secure_cookies == "auto":
            # HTTPS as reported by a trusted reverse proxy. Not for the direct :8443 listener: a
            # Secure cookie there would shadow the plain-HTTP UI on :8090 for the same host.
            return request.url.scheme == "https" and "x-forwarded-proto" in request.headers
        return settings.secure_cookies == "true"

    def set_cookie(request: Request, resp: Response) -> None:
        s = store.config.settings
        resp.set_cookie(auth.COOKIE, auth.make_session(s.session_secret, s.admin_password_hash),
                        max_age=auth.SESSION_TTL, httponly=True, samesite="strict",
                        secure=cookie_secure(request))

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": settings.version}

    @app.get("/api/session")
    async def session(request: Request):
        return {"authenticated": authed(request),
                "setup_required": not store.config.settings.admin_password_hash,
                "version": settings.version, "https_port": settings.https_port}

    @app.post("/api/setup")
    async def setup(body: PasswordBody, request: Request, response: Response):
        if store.config.settings.admin_password_hash:
            raise HTTPException(409, "already set up")
        if len(body.password) < auth.MIN_PASSWORD_LENGTH:
            raise HTTPException(400, f"password must be at least {auth.MIN_PASSWORD_LENGTH} characters")
        pw_hash = await asyncio.to_thread(auth.hash_password, body.password)
        if store.config.settings.admin_password_hash:     # a parallel setup won the race
            raise HTTPException(409, "already set up")
        store.config.settings.admin_password_hash = pw_hash
        store.save()
        log.info("admin password set from %s", client_ip(request))
        set_cookie(request, response)
        return {"ok": True}

    @app.post("/api/login")
    async def login(body: PasswordBody, request: Request, response: Response):
        h = store.config.settings.admin_password_hash
        if not h:
            raise HTTPException(401, "wrong password")
        ip = client_ip(request)
        charge_attempt(ip)
        if not await password_ok(body.password, h):
            log.warning("failed admin login from %s", ip)
            raise HTTPException(401, "wrong password")
        throttle.success(ip)
        set_cookie(request, response)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        response.delete_cookie(auth.COOKIE, httponly=True, samesite="strict", secure=cookie_secure(request))
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
            msg = str(first.get("msg", "")).removeprefix("Value error, ")
            raise HTTPException(422, f"{field}: {msg}" if field else msg) from e

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
        check_sounds([cam.notify_sound])
        cfg.cameras.append(cam)
        await save_and_apply()
        log.info("camera '%s' added", cam.name)
        engine.probe_later(cam)
        return redact(cam)

    @app.put("/api/cameras/{cid}")
    async def update_camera(cid: str, payload: dict):
        old = find(cfg.cameras, cid)
        cam = merge(Camera, old, payload)
        check_sounds([cam.notify_sound])
        cfg.cameras[cfg.cameras.index(old)] = cam
        engine.probes.pop(cid, None)
        await save_and_apply()
        engine.probe_later(cam)
        return redact(cam)

    @app.delete("/api/cameras/{cid}")
    async def delete_camera(cid: str):
        cam = find(cfg.cameras, cid)
        used = [b.name or b.id for b in cfg.bridges if cid in b.camera_ids()]
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

    @app.post("/api/cameras/{cid}/test-notice")
    async def test_notice(cid: str, body: NoticeBody):
        """Play the call notice (saved or the values in the body) on the camera speaker."""
        cam = find(cfg.cameras, cid)
        try:
            return await engine.test_notice(
                cam, body.notify_text if body.notify_text is not None else cam.notify_text,
                body.notify_sound if body.notify_sound is not None else cam.notify_sound,
                body.notify_voice or cam.notify_voice, body.notify_speed or cam.notify_speed)
        except CameraBusy as e:
            raise HTTPException(409, str(e)) from e
        except Exception as e:
            raise HTTPException(502, f"notice test failed: {e or 'timeout'}") from e

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
    def check_bridge(b: Bridge) -> None:
        check_sounds(b.sound_ids())
        for cid in b.camera_ids():
            if not store.camera(cid):
                raise HTTPException(422, "camera_id: unknown camera")
        if not store.phone(b.phone_id):
            raise HTTPException(422, "phone_id: unknown phone")
        conflict = store.routing_conflict(b)
        if conflict:
            raise HTTPException(409, conflict)

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
        check_bridge(b)
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
            call = await engine.dial(bid, target, body.camera_id or None)
        except CameraBusy as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"call_id": call.id}

    # -- IVR prompts --------------------------------------------------------------------------------
    @app.get("/api/ivr/voices")
    async def ivr_voices():
        return {"available": engine.tts.available, "voices": await engine.tts.voices()}

    @app.post("/api/ivr/preview")
    async def ivr_preview(body: IvrPreviewBody):
        """Render the menu (or a single prompt) as a WAV file for the browser."""
        body.ivr_speed = max(80, min(300, body.ivr_speed))
        body.ivr_voice = body.ivr_voice or "en-us"
        if body.text is not None or body.sound:
            pcm = await engine.prompt_pcm((body.text or "")[:2000], body.sound, body.ivr_voice, body.ivr_speed)
        else:
            pcm = await engine.menu_pcm(body)
        return Response(to_wav(pcm), media_type="audio/wav", headers={"Cache-Control": "no-store"})

    # -- text-to-speech voices ---------------------------------------------------------------
    def voice_users(voice: str) -> list[str]:
        users = [f"camera '{c.name}'" for c in cfg.cameras if c.notify_voice == voice]
        users += [f"bridge '{b.name or b.id}'" for b in cfg.bridges if b.ivr_voice == voice]
        return users

    @app.get("/api/tts/voices")
    async def tts_voices():
        """Natural (Piper) voices - installed, downloading and the catalog - plus espeak-ng voices."""
        t = engine.tts
        piper = t.piper
        out = {"espeak": {"available": t.available, "voices": await t.voices()},
               "piper": {"available": bool(piper and piper.available)}}
        if piper and piper.available:
            installed = piper.installed()
            for v in installed:
                v["used_by"] = voice_users(tts_mod.PIPER_PREFIX + v["key"])
            try:
                catalog = [piper_mod.simplify(k, v) for k, v in (await piper.catalog()).items()]
            except Exception:
                catalog = []
            out["piper"].update(installed=installed, downloads=piper.downloads,
                                catalog=catalog, catalog_error=piper.catalog_error)
        return out

    @app.post("/api/tts/voices/{key}")
    async def download_voice(key: str):
        piper = engine.tts.piper
        if not piper or not piper.available:
            raise HTTPException(409, "Piper is not available in this installation")
        try:
            known = key in await piper.catalog()
        except Exception as e:
            raise HTTPException(502, str(e)) from e
        if not known:
            raise HTTPException(404, "unknown voice")
        piper.start_download(key)
        return {"ok": True, "installed": piper.is_installed(key)}

    @app.delete("/api/tts/voices/{key}")
    async def delete_voice(key: str):
        piper = engine.tts.piper
        if not piper or not piper.is_installed(key):
            raise HTTPException(404, "not installed")
        users = voice_users(tts_mod.PIPER_PREFIX + key)
        if users:
            raise HTTPException(409, f"voice is used by {', '.join(users)}")
        piper.delete(key)
        return {"ok": True}

    # -- sounds ---------------------------------------------------------------------------------
    def sound_users(sid: str) -> list[str]:
        users = [f"camera '{c.name}'" for c in cfg.cameras if c.notify_sound == sid]
        users += [f"bridge '{b.name or b.id}'" for b in cfg.bridges if sid in b.sound_ids()]
        return users

    def check_sounds(ids) -> None:
        for sid in ids:
            if sid and not engine.sounds.get(sid):
                raise HTTPException(422, "unknown sound - it may have been deleted")

    @app.get("/api/sounds")
    async def list_sounds():
        return [dict(s.model_dump(), used_by=sound_users(s.id)) for s in cfg.sounds]

    @app.post("/api/sounds")
    async def upload_sound(request: Request, name: str = "sound"):
        """Body: a PCM WAV file (the web UI converts other formats in the browser)."""
        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large")
        data = await request.body()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large")
        try:
            sound = engine.sounds.add(name, data)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return sound.model_dump()

    @app.put("/api/sounds/{sid}")
    async def rename_sound(sid: str, body: RenameBody):
        sound = engine.sounds.get(sid)
        if not sound:
            raise HTTPException(404, "not found")
        sound.name = body.name.strip()[:80] or sound.name
        store.save()
        return sound.model_dump()

    @app.delete("/api/sounds/{sid}")
    async def delete_sound(sid: str):
        if not engine.sounds.get(sid):
            raise HTTPException(404, "not found")
        users = sound_users(sid)
        if users:
            raise HTTPException(409, f"sound is used by {', '.join(users)}")
        engine.sounds.delete(sid)
        return {"ok": True}

    @app.get("/api/sounds/{sid}.wav")
    async def sound_file(sid: str):
        pcm = engine.sounds.load(sid)
        if pcm is None:
            raise HTTPException(404, "not found")
        return Response(to_wav(pcm), media_type="audio/wav", headers={"Cache-Control": "no-store"})

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
    async def change_password(body: ChangePasswordBody, request: Request, response: Response):
        s = cfg.settings
        if len(body.new) < auth.MIN_PASSWORD_LENGTH:
            raise HTTPException(400, f"password must be at least {auth.MIN_PASSWORD_LENGTH} characters")
        ip = client_ip(request)
        charge_attempt(ip)
        if not await password_ok(body.current, s.admin_password_hash):
            log.warning("wrong current password in password change from %s", ip)
            raise HTTPException(401, "current password is wrong")
        throttle.success(ip)
        s.admin_password_hash = await asyncio.to_thread(auth.hash_password, body.new)
        store.save()
        set_cookie(request, response)
        return {"ok": True}

    @app.post("/api/settings/api-token")
    async def regenerate_token():
        cfg.settings.api_token = secrets.token_urlsafe(24)
        store.save()
        return {"api_token": cfg.settings.api_token}

    # -- browser calls (WebSockets) ---------------------------------------------------------------
    def ws_authed(ws: WebSocket) -> bool:
        # The HTTP middleware doesn't run for WebSockets, and CORS doesn't cover them. SameSite=strict
        # keeps the cookie off cross-site handshakes, but not off handshakes from sibling subdomains
        # (same site), so check who opened the socket: Sec-Fetch-Site when the browser sends it
        # (browser-computed, can't be misread through a proxy), else the Origin header.
        s = store.config.settings
        if cross_site(ws.headers):
            return False
        if "sec-fetch-site" not in ws.headers and foreign_origin(ws.headers):
            return False
        return auth.check_session(ws.cookies.get(auth.COOKIE), s.session_secret, s.admin_password_hash)

    async def ws_reject(ws: WebSocket, message: str, code: int) -> None:
        await ws.send_text(json.dumps({"type": "error", "message": message}))
        await ws.close(code=code)

    @app.websocket("/api/cameras/{cid}/talk")
    async def ws_talk(ws: WebSocket, cid: str):
        await ws.accept()
        if not ws_authed(ws):
            return await ws_reject(ws, "not authenticated", 4401)
        cam = store.camera(cid)
        if not cam or not cam.enabled:
            return await ws_reject(ws, "camera not found or disabled", 4404)
        client = ws.client.host if ws.client else "?"
        try:
            await engine.run_web_call(cam, ws, client)
        except CameraBusy as e:
            await ws_reject(ws, str(e), 4409)

    @app.websocket("/api/cameras/{cid}/video")
    async def ws_video(ws: WebSocket, cid: str):
        """Proxy go2rtc's MSE (fMP4 over WebSocket) for one camera stream only."""
        await ws.accept()
        if not ws_authed(ws):
            return await ws_reject(ws, "not authenticated", 4401)
        cam = store.camera(cid)
        if not cam or not cam.enabled:
            return await ws_reject(ws, "camera not found or disabled", 4404)
        url = settings.go2rtc_api.replace("http://", "ws://", 1).rstrip("/") + f"/api/ws?src={cam.stream_name}"
        try:
            upstream = await websockets.connect(url, max_size=None, open_timeout=5, ping_interval=None)
        except Exception as e:
            return await ws_reject(ws, f"go2rtc unavailable: {e}", 4502)

        async def down():
            async for m in upstream:
                if isinstance(m, bytes):
                    await ws.send_bytes(m)
                else:
                    await ws.send_text(m)

        async def up():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                text = msg.get("text")
                if not text:
                    continue
                try:
                    kind = json.loads(text).get("type")
                except (ValueError, AttributeError):
                    continue
                if kind == "mse":   # only the MSE request is forwarded to go2rtc
                    await upstream.send(text)

        tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await upstream.close()
            with contextlib.suppress(Exception):
                await ws.close()

    # -- UI -------------------------------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app


class _NoSignalServer(uvicorn.Server):
    """uvicorn server that leaves signal handling to the main server."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield
