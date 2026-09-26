"""The bridge engine: glues SIP calls to camera audio through go2rtc."""

from __future__ import annotations

import array
import asyncio
import logging
import secrets
import time

import httpx

from .go2rtc import Go2rtc, summarize_probe
from .media import g711, tts
from .media.dtmf import DtmfDetector
from .media.rtp import Pacer, PortAllocator, RtpPacket
from .media.rtsp import RtspAudioClient, TalkServer
from .models import Bridge, Camera
from .settings import Settings
from .sip.ua import AccountConfig, Call, UserAgent
from .sounds import SoundLibrary
from .store import Store
from .webcall import WebCall

log = logging.getLogger("cam2sip.engine")

DTMF_EVENTS = "0123456789*#ABCD"
RTP_TIMEOUT = 60.0
OUTBOUND_RING_TIMEOUT = 60.0


class CameraBusy(Exception):
    pass


def mic_query(camera: Camera) -> str:
    return ("video&" if camera.mic_with_video else "") + "audio=pcma,pcmu"


class CameraLink:
    """Camera side of any call: microphone in, speaker out (both through go2rtc).

    Used by SIP calls (BridgeSession) and browser calls (WebCall). The owner
    gets microphone audio through `on_mic(codec, payload)` and feeds far-end
    audio with `speak()`.
    """

    def __init__(self, engine: "Engine", camera: Camera, on_mic):
        self.engine = engine
        self.camera = camera
        self.on_mic = on_mic
        self.mic: RtspAudioClient | None = None
        self.talk = None
        self.talk_state = "none"
        self.tasks: list[asyncio.Task] = []
        self._gate_open_until = 0.0
        self._last_talk_push = 0.0
        # privacy notice: the microphone stays closed until the notice has played
        self.notice_state = "pending" if camera.notify_enabled else "none"
        self.mic_open = not camera.notify_enabled

    def start(self) -> None:
        cam = self.camera
        url = self.engine.go2rtc.rtsp_url(cam.stream_name, mic_query(cam))
        self.mic = RtspAudioClient(url, self._mic_audio)
        self.tasks.append(asyncio.create_task(self.mic.run()))
        if cam.talk_source():
            self.talk = self.engine.talk_server.create()
            self.tasks.append(asyncio.create_task(self._talk_loop()))
            self.tasks.append(asyncio.create_task(self._keepalive()))

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        self.tasks = []
        if self.talk:
            self.talk.close()
            await self.engine.go2rtc.stop_play(self.camera.talk_stream_name)

    @property
    def speaking(self) -> bool:
        return time.monotonic() < self._gate_open_until

    def _mic_audio(self, codec: str, payload: bytes) -> None:
        if self.mic_open:
            self.on_mic(codec, payload)

    # -- privacy notice ----------------------------------------------------------------
    def start_notice(self) -> None:
        """Call when the call is connected: plays the camera's call notice, then opens the mic."""
        if self.notice_state == "pending" and not any(t.get_name() == "notice" for t in self.tasks):
            self.tasks.append(asyncio.create_task(self._notice(), name="notice"))

    async def _notice(self) -> None:
        cam = self.camera
        played = False
        try:
            pcm = await self.engine.prompt_pcm(cam.notify_text, cam.notify_sound, cam.notify_voice, cam.notify_speed)
            talk = self.talk
            if not talk:
                log.warning("[%s] call notice skipped: camera has no speaker source", cam.name)
                return
            try:
                await asyncio.wait_for(talk.connected.wait(), 10)
            except TimeoutError:
                log.warning("[%s] call notice skipped: speaker did not connect", cam.name)
                return
            self.notice_state = "playing"
            codec = talk.codec or "PCMA"
            data = g711.encode_pcm(pcm, codec)
            loop = asyncio.get_running_loop()
            t = loop.time()
            for i in range(0, len(data), 160):
                frame = data[i:i + 160]
                self._push(frame, time.monotonic())
                self.on_mic(codec, frame)            # the far end hears the notice too
                t += len(frame) / 8000
                await asyncio.sleep(max(0.0, t - loop.time()))
            await asyncio.sleep(0.5)                 # let the camera finish playing its buffer
            played = True
            log.info("[%s] call notice played (%.1f s)", cam.name, len(pcm) / 8000)
        except Exception as e:
            log.warning("[%s] call notice failed: %s", cam.name, e)
        finally:
            self.notice_state = "played" if played else "skipped"
            self.mic_open = True

    def speak(self, payload: bytes, codec: str, gain_db: float = 0.0, gate_db: float | None = None) -> None:
        """Send one far-end audio frame towards the camera speaker (noise-gated)."""
        talk = self.talk
        if not talk or not talk.codec or self.notice_state in ("pending", "playing"):
            return
        now = time.monotonic()
        if gate_db is None or g711.level_dbfs(payload, codec) >= gate_db:
            self._gate_open_until = now + 0.6   # hangover keeps word endings
        if now < self._gate_open_until:
            self._push(g711.convert(payload, codec, talk.codec, gain_db), now)

    def _push(self, payload: bytes, now: float) -> None:
        gap = now - self._last_talk_push if self._last_talk_push else 0.0
        if gap > 0.1:
            self.talk.advance(int(gap * 8000) - len(payload))
        self.talk.push(payload)
        self._last_talk_push = now

    async def _keepalive(self) -> None:
        # go2rtc drops an RTSP source after 5 s without data: while nobody talks,
        # send one silent frame per second (inaudible, doesn't trigger the camera's AEC)
        while True:
            await asyncio.sleep(0.5)
            talk = self.talk
            now = time.monotonic()
            if talk and talk.codec and talk.connected.is_set() and now - self._last_talk_push > 1.0:
                self._push(g711.silence(talk.codec, 160), now)

    async def _talk_loop(self) -> None:
        """Ask go2rtc to pull our talk stream into the camera; re-attach if it drops."""
        go2rtc = self.engine.go2rtc
        dst = self.camera.talk_stream_name
        attempt = 0
        while True:
            if not self.talk.connected.is_set():
                attempt += 1
                self.talk_state = "connecting"
                try:
                    await go2rtc.play(dst, self.talk.url)
                    await asyncio.wait_for(self.talk.connected.wait(), 10)
                    self.talk_state = "connected"
                    attempt = 0
                    log.info("[%s] speaker connected (%s)", self.camera.name, self.talk.codec)
                except Exception as e:
                    self.talk_state = "error"
                    log.warning("[%s] speaker connect failed: %s", self.camera.name, e or "timeout")
                    await asyncio.sleep(min(2 * attempt, 10))
                    continue
            await asyncio.sleep(1)

    def info(self) -> dict:
        return {
            "mic": {
                "connected": bool(self.mic and self.mic.connected.is_set()),
                "codec": self.mic.codec if self.mic else None,
                "error": self.mic.error if self.mic else None,
                "muted": not self.mic_open,
            },
            "notice": self.notice_state,
            "speaker": {
                "state": self.talk_state if self.talk else "none",
                "codec": self.talk.codec if self.talk else None,
                "packets": self.talk.tx_packets if self.talk else 0,
                "gate_open": self.speaking,
            },
        }


class BridgeSession:
    """One SIP call on a bridge: camera audio <-> phone, plus the optional IVR menu.

    Phases: "menu" (IVR prompt playing / waiting for a digit) and "connected"
    (a camera is attached through a CameraLink). In IVR mode the caller can go
    back to the menu with the bridge's menu digit and pick another camera.
    """

    def __init__(self, engine: "Engine", bridge: Bridge, call: Call, camera: Camera | None = None):
        self.engine = engine
        self.bridge = bridge
        self.call = call
        self.initial_camera = camera     # direct mode / outbound: connect right away
        self.camera: Camera | None = None
        self.link: CameraLink | None = None
        self.phase = "starting"
        self.pacer: Pacer | None = None
        self.tasks: list[asyncio.Task] = []
        self.visited: list[str] = []
        self._prompt = b""
        self._prompt_pos = 0
        self._prompt_done = asyncio.Event()
        self._prompt_done.set()
        self._digits: asyncio.Queue[str] = asyncio.Queue()
        self._last_dtmf_ts: int | None = None
        self._inband: DtmfDetector | None = None
        self.started = time.time()

    @property
    def phone_codec(self) -> str:
        return self.call.codec or "PCMA"

    async def start(self) -> None:
        call = self.call
        self.pacer = Pacer(self._to_phone, g711.SILENCE.get(self.phone_codec, 0xD5))
        self.pacer.start()
        if call.rtp:
            call.rtp.on_packet = self._on_phone_rtp
        call.on_dtmf = self._on_dtmf
        self.tasks.append(asyncio.create_task(self._watchdog()))
        if self.initial_camera:
            self._connect(self.initial_camera)
        else:
            self._start_menu()

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        if self.pacer:
            self.pacer.stop()
        await self._disconnect()

    # -- camera attach / detach -------------------------------------------------------
    def _connect(self, camera: Camera, auto_notice: bool = True) -> None:
        """Attach a camera (the caller must already hold engine.busy[camera.id])."""
        self.camera = camera
        self.link = CameraLink(self.engine, camera, self._on_mic)
        self.link.start()
        if self.pacer:
            self.pacer.buf.clear()
            self.pacer.primed = False
        self.phase = "connected"
        if not self.visited or self.visited[-1] != camera.name:
            self.visited.append(camera.name)
        if auto_notice:
            self.tasks.append(asyncio.create_task(self._begin_notice(self.link)))

    async def _begin_notice(self, link: CameraLink) -> None:
        # the notice starts once the call is up and our own prompts have finished
        await self.call.answered.wait()
        await self._prompt_done.wait()
        if self.link is link:
            link.start_notice()

    async def _disconnect(self) -> None:
        link, cam = self.link, self.camera
        self.link, self.camera = None, None
        if link:
            await link.stop()
        if cam and self.engine.busy.get(cam.id) is self.call:
            del self.engine.busy[cam.id]

    # -- camera -> phone (with prompts taking priority) -------------------------------------
    def _on_mic(self, codec: str, payload: bytes) -> None:
        if self.pacer and self.call.codec and self.phase == "connected":
            self.pacer.push(g711.convert(payload, codec, self.phone_codec, self.bridge.mic_gain_db))

    def _to_phone(self, frame: bytes) -> None:
        if self._prompt:
            chunk = self._prompt[self._prompt_pos:self._prompt_pos + 160]
            self._prompt_pos += 160
            if self._prompt_pos >= len(self._prompt):
                self._stop_prompt()
            if len(chunk) < 160:
                chunk += bytes([g711.SILENCE.get(self.phone_codec, 0xD5)]) * (160 - len(chunk))
            frame = chunk
        call = self.call
        if call.state == "active" and call.rtp and call.negotiated:
            call.rtp.send(call.negotiated.remote_pt, frame, len(frame))

    def _play(self, audio: bytes) -> None:
        self._prompt, self._prompt_pos = audio, 0
        if audio:
            self._prompt_done.clear()
        else:
            self._prompt_done.set()

    def _stop_prompt(self) -> None:
        self._prompt = b""
        self._prompt_done.set()

    async def _say(self, text: str, sound: str = "") -> None:
        if not text.strip() and not sound:
            return
        b = self.bridge
        pcm = await self.engine.prompt_pcm(text, sound, b.ivr_voice, b.ivr_speed)
        self._play(g711.encode_pcm(pcm, self.phone_codec))
        await self._prompt_done.wait()

    # -- IVR menu ----------------------------------------------------------------------------
    def _label(self, opt) -> str:
        cam = self.engine.store.camera(opt.camera_id)
        return opt.label.strip() or (cam.name if cam else f"camera {opt.digit}")

    def _start_menu(self) -> None:
        self.phase = "menu"
        while not self._digits.empty():
            self._digits.get_nowait()
        self.tasks.append(asyncio.create_task(self._menu_loop()))

    async def _wait_digit(self, timeout: float) -> str | None:
        try:
            return await asyncio.wait_for(self._digits.get(), timeout)
        except TimeoutError:
            return None

    async def _menu_loop(self) -> None:
        b = self.bridge
        engine = self.engine
        await self.call.answered.wait()
        await asyncio.sleep(0.4)          # let the media path settle before speaking
        options = {o.digit: o for o in b.ivr_options}
        menu_audio = g711.encode_pcm(await engine.menu_pcm(b), self.phone_codec)
        for _ in range(b.ivr_repeats):
            if self.call.state != "active":
                return
            if self._digits.empty():          # a key pressed during the last prompt skips the menu
                self._play(menu_audio)
            digit = await self._wait_digit(len(menu_audio) / 8000 + b.ivr_timeout)
            if digit is None:
                continue
            self._stop_prompt()
            opt = options.get(digit)
            cam = engine.store.camera(opt.camera_id) if opt else None
            if not opt or not cam or not cam.enabled:
                log.info("IVR: invalid choice %s", digit)
                await self._say(b.ivr_invalid_text, b.ivr_invalid_sound)
                continue
            name = self._label(opt)
            if cam.id in engine.busy:
                log.info("IVR: %s is busy", cam.name)
                await self._say(tts.fill(b.ivr_busy_text, name=name), b.ivr_busy_sound)
                continue
            engine.busy[cam.id] = self.call
            log.info("IVR: caller %s chose %s -> %s", self.call.remote_user or "?", digit, cam.name)
            self._connect(cam, auto_notice=False)   # camera connects while the confirmation plays
            await self._say(tts.fill(b.ivr_connect_text, name=name), b.ivr_connect_sound)
            if self.link:
                self.link.start_notice()           # then the privacy notice (if enabled)
            return
        if self.call.state == "active":
            await self._say(b.ivr_goodbye_text, b.ivr_goodbye_sound)
            await self.call.hangup("no menu selection")

    async def _back_to_menu(self) -> None:
        if self.phase != "connected":
            return
        self.phase = "menu"
        await self._disconnect()
        self._start_menu()

    # -- phone -> camera -----------------------------------------------------------------------
    def _on_phone_rtp(self, pkt: RtpPacket) -> None:
        call = self.call
        neg = call.negotiated
        if not neg or call.state != "active":
            return
        if neg.dtmf_pt is not None and pkt.pt == neg.dtmf_pt:
            self._rtp_dtmf(pkt)
            return
        if pkt.pt != neg.remote_pt:
            return
        if neg.dtmf_pt is None:          # no RFC 4733 negotiated: listen for key tones in the audio
            if self._inband is None:
                self._inband = DtmfDetector()
            table = g711.DECODE[self.phone_codec]
            digit = self._inband.feed([table[x] for x in pkt.payload])
            if digit:
                self._on_dtmf(digit)
        if self.phase == "connected" and self.link:
            self.link.speak(pkt.payload, self.phone_codec, self.bridge.speaker_gain_db,
                            self.bridge.speaker_gate_db)

    # -- dtmf / watchdog ----------------------------------------------------------------------
    def _rtp_dtmf(self, pkt: RtpPacket) -> None:
        if len(pkt.payload) < 4 or pkt.ts == self._last_dtmf_ts:
            return
        self._last_dtmf_ts = pkt.ts
        event = pkt.payload[0]
        if event < len(DTMF_EVENTS):
            self._on_dtmf(DTMF_EVENTS[event])

    def _on_dtmf(self, digit: str) -> None:
        log.info("[%s] DTMF %s", self.camera.name if self.camera else "menu", digit)
        if self.phase == "menu":
            self._digits.put_nowait(digit)
            self._stop_prompt()               # barge-in: a key press cuts any prompt short
            return
        b = self.bridge
        if b.mode == "ivr" and b.menu_digit and digit == b.menu_digit:
            asyncio.create_task(self._back_to_menu())
            return
        hang = b.hangup_digit and digit == b.hangup_digit
        for action in b.dtmf_actions:
            if action.digit == digit:
                asyncio.create_task(self._fire(action))
                hang = hang or action.hangup
        if hang:
            asyncio.create_task(self.call.hangup("DTMF hangup"))

    async def _fire(self, action) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.request(action.method, action.url, content=action.body or None)
            log.info("DTMF %s action -> %s %s: HTTP %d", action.digit, action.method, action.url, r.status_code)
        except Exception as e:
            log.warning("DTMF %s action failed: %s", action.digit, e)

    async def _watchdog(self) -> None:
        call = self.call
        while call.state != "ended":
            await asyncio.sleep(1)
            if call.state != "active" or not call.answered_at:
                continue
            if time.time() - call.answered_at > self.bridge.max_call_seconds:
                await call.hangup("max call duration reached")
                return
            rtp = call.rtp
            last = rtp.last_rx if rtp and rtp.last_rx else None
            if time.time() - call.answered_at > RTP_TIMEOUT and (
                    last is None or time.monotonic() - last > RTP_TIMEOUT):
                await call.hangup("no RTP from phone")
                return

    def info(self) -> dict:
        if self.link:
            info = self.link.info()
            info["mic"]["underruns"] = self.pacer.underruns if self.pacer else 0
            info["mic"]["buffer_ms"] = int(len(self.pacer.buf) / 8) if self.pacer else 0
        else:
            info = {"mic": {"connected": False, "codec": None, "error": None},
                    "speaker": {"state": "none", "codec": None, "packets": 0, "gate_open": False}}
        info["phase"] = self.phase
        return info


class Engine:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.ports = PortAllocator(settings.rtp_port_min, settings.rtp_port_max)
        self.ua = UserAgent(settings.sip_port, self.ports, settings.advertise_ip,
                            user_agent=f"cam2sip/{settings.version}", bind=settings.sip_bind)
        self.ua.on_incoming = self.on_incoming
        self.go2rtc = Go2rtc(settings.go2rtc_api, settings.go2rtc_rtsp)
        self.talk_server = TalkServer("127.0.0.1", settings.talk_port)
        self.tts = tts.Tts()
        self.sounds = SoundLibrary(store)
        self.busy: dict[str, Call] = {}                # camera id -> call
        self.sessions: dict[str, BridgeSession] = {}   # call id -> session
        self.call_meta: dict[str, dict] = {}           # call id -> bridge/camera/phone ids
        self.probes: dict[str, dict] = {}              # camera id -> last probe result
        self.web_calls: dict[str, WebCall] = {}        # web call id -> browser call
        self.started = time.time()
        self._tasks: list[asyncio.Task] = []

    # -- lifecycle ------------------------------------------------------------------------
    async def start(self) -> None:
        await self.ua.start()
        await self.talk_server.start()
        await self.apply_config()
        self._tasks.append(asyncio.create_task(self.go2rtc.run()))
        for cam in self.store.config.cameras:
            if cam.enabled:
                self.probe_later(cam)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for wc in list(self.web_calls.values()):
            await wc.hangup("server shutting down")
        await self.ua.stop()
        await self.talk_server.stop()
        await self.go2rtc.close()

    async def apply_config(self) -> None:
        cfg = self.store.config
        desired: dict[str, list[str]] = {}
        for cam in cfg.cameras:
            if not cam.enabled:
                continue
            mic = cam.mic_source()
            if mic:
                # second source transcodes non-G.711 microphones on demand
                desired[cam.stream_name] = [mic, f"ffmpeg:{cam.stream_name}#audio=pcma"]
            talk = cam.talk_source()
            if talk:
                desired[cam.talk_stream_name] = [talk]
        await self.go2rtc.sync(desired)
        accounts = [
            AccountConfig(id=p.id, server=p.server, port=p.port, username=p.username,
                          password=p.password, auth_username=p.auth_username, domain=p.domain,
                          display_name=p.display_name, expires=p.expires, contact_user=p.contact_user)
            for p in cfg.phones if p.enabled
        ]
        await self.ua.set_accounts(accounts)
        asyncio.create_task(self._prewarm_prompts())

    async def _prewarm_prompts(self) -> None:
        """Synthesize IVR menus and call notices ahead of time so callers don't wait for TTS."""
        try:
            for b in self.store.config.bridges:
                if b.mode == "ivr" and b.enabled:
                    await self.menu_pcm(b)
            for cam in self.store.config.cameras:
                if cam.notify_enabled:
                    await self.prompt_pcm(cam.notify_text, cam.notify_sound, cam.notify_voice, cam.notify_speed)
        except Exception as e:
            log.debug("prompt prewarm failed: %s", e)

    # -- prompts (text-to-speech or uploaded sounds) ----------------------------------------
    async def prompt_pcm(self, text: str, sound: str, voice: str, speed: int):
        """8 kHz samples of an uploaded sound, or of `text` spoken by TTS."""
        pcm = self.sounds.load(sound) if sound else None
        if pcm is not None:
            return pcm
        if sound:
            log.warning("sound %s not found - using text-to-speech", sound)
        return await self.tts.pcm(text, voice, speed)

    async def menu_pcm(self, b) -> "array.array":
        """The full IVR menu: greeting + one line per option (TTS and/or uploaded sounds)."""
        def label(o) -> str:
            cam = self.store.camera(o.camera_id)
            return o.label.strip() if o.label.strip() else (cam.name if cam else f"camera {o.digit}")
        if not b.ivr_greeting_sound and not any(o.sound for o in b.ivr_options):
            text = tts.menu_text(b.ivr_greeting, b.ivr_option_text, [(o.digit, label(o)) for o in b.ivr_options])
            return await self.tts.pcm(text, b.ivr_voice, b.ivr_speed)
        gap = array.array("h", bytes(2 * 2400))          # 0.3 s between parts
        out = array.array("h")
        parts = [(b.ivr_greeting, b.ivr_greeting_sound)]
        parts += [(tts.fill(b.ivr_option_text, digit=o.digit, name=label(o)), o.sound) for o in b.ivr_options]
        for text, sound in parts:
            if not text.strip() and not sound:
                continue
            if out:
                out.extend(gap)
            out.extend(await self.prompt_pcm(text, sound, b.ivr_voice, b.ivr_speed))
        return out

    # -- calls ----------------------------------------------------------------------------
    def _phone_for_account(self, account_id: str):
        return self.store.phone(account_id)

    async def on_incoming(self, call: Call) -> None:
        phone = self._phone_for_account(call.account.cfg.id)
        bridge = self.store.bridge_for_phone(phone.id) if phone else None
        who = call.remote_user or "unknown"
        if not bridge or not bridge.enabled:
            log.info("call from %s to %s rejected: no active bridge", who, call.account.cfg.username)
            call.reject(480, "Temporarily Unavailable")
            self._record(call, bridge)
            return
        if bridge.allowed_callers and who not in bridge.allowed_callers:
            log.info("call from %s rejected: caller not allowed on bridge %s", who, bridge.name)
            call.reject(403, "Forbidden")
            self._record(call, bridge)
            return
        camera = None
        if bridge.mode == "direct":
            camera = self.store.camera(bridge.camera_id)
            if not camera or not camera.enabled:
                log.info("call from %s rejected: camera missing or disabled", who)
                call.reject(480, "Temporarily Unavailable")
                self._record(call, bridge)
                return
            if camera.id in self.busy:
                log.info("call from %s rejected: camera %s busy", who, camera.name)
                call.reject(486, "Busy Here")
                self._record(call, bridge, [camera.name])
                return
            self.busy[camera.id] = call
        session = BridgeSession(self, bridge, call, camera)
        self._attach(call, session, bridge)
        call.ring()
        await session.start()   # connect camera audio (or prepare the menu) while ringing
        if bridge.answer_delay > 0:
            try:
                await asyncio.wait_for(asyncio.shield(call.ended.wait()), bridge.answer_delay)
            except TimeoutError:
                pass
        if call.state == "ringing":
            await call.answer()
            log.info("call from %s answered on bridge '%s' (%s, %s)", who, bridge.name or bridge.id,
                     f"camera {camera.name}" if camera else "IVR menu", call.codec)

    async def dial(self, bridge_id: str, target: str, camera_id: str | None = None) -> Call:
        bridge = self.store.bridge(bridge_id)
        if not bridge:
            raise KeyError("bridge not found")
        ids = bridge.camera_ids()
        cid = camera_id or ids[0]
        if cid not in ids:
            raise ValueError("that camera is not part of this bridge")
        camera = self.store.camera(cid)
        account = self.ua.accounts.get(bridge.phone_id)
        if not camera or not account:
            raise ValueError("bridge camera or phone is missing/disabled")
        if account.state != "registered":
            raise ValueError(f"phone is not registered ({account.state})")
        if camera.id in self.busy:
            raise CameraBusy(f"camera {camera.name} is already in a call")
        call = await account.dial(target)
        self.busy[camera.id] = call
        session = BridgeSession(self, bridge, call, camera)
        self._attach(call, session, bridge)
        await session.start()
        asyncio.create_task(self._ring_timeout(call))
        log.info("calling %s from bridge '%s' (camera %s)", target, bridge.name or bridge.id, camera.name)
        return call

    async def _ring_timeout(self, call: Call) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(call.answered.wait()), OUTBOUND_RING_TIMEOUT)
        except TimeoutError:
            if call.state == "ringing":
                await call.hangup("no answer")

    def _attach(self, call: Call, session: BridgeSession, bridge: Bridge) -> None:
        self.sessions[call.id] = session
        self.call_meta[call.id] = {"bridge": bridge}

        def ended(c: Call) -> None:
            asyncio.create_task(self._cleanup(c))
        call.on_end = ended
        if call.state == "ended":
            ended(call)

    async def _cleanup(self, call: Call) -> None:
        session = self.sessions.pop(call.id, None)
        meta = self.call_meta.pop(call.id, {})
        if session:
            await session.stop()          # releases the camera it holds
        for cid, holder in list(self.busy.items()):
            if holder is call:
                del self.busy[cid]
        self._record(call, meta.get("bridge"), session.visited if session else None)

    def _record(self, call: Call, bridge: Bridge | None, cameras: list[str] | None = None) -> None:
        info = call.info()
        dur = int(info["ended_at"] - info["answered_at"]) if info["answered_at"] and info["ended_at"] else 0
        self.store.add_history({
            "id": call.id, "direction": call.direction, "remote": call.remote_user,
            "remote_display": call.remote_display, "phone": call.account.cfg.username,
            "bridge": (bridge.name or bridge.id) if bridge else None,
            "camera": ", ".join(cameras) if cameras else None,
            "started_at": info["created_at"], "answered_at": info["answered_at"],
            "ended_at": info["ended_at"] or time.time(), "duration": dur,
            "codec": info["codec"], "result": call.end_reason or "rejected",
        })

    # -- browser calls ------------------------------------------------------------------------
    async def run_web_call(self, camera: Camera, ws, client: str) -> None:
        """Serve a browser call to `camera` on an accepted WebSocket."""
        if camera.id in self.busy:
            raise CameraBusy(f"{camera.name} is already in a call")
        call = WebCall(self, camera, ws, client)
        self.busy[camera.id] = call
        self.web_calls[call.id] = call
        try:
            await call.run()
        finally:
            self.web_calls.pop(call.id, None)
            if self.busy.get(camera.id) is call:
                del self.busy[camera.id]
            info = call.info()
            self.store.add_history({
                "id": call.id, "direction": "web", "remote": client, "remote_display": "Web UI",
                "phone": "-", "bridge": None, "camera": camera.name,
                "started_at": call.started_at, "answered_at": call.started_at,
                "ended_at": call.ended_at or time.time(),
                "duration": int((call.ended_at or time.time()) - call.started_at),
                "codec": info["codec"], "result": call.end_reason,
            })

    async def hangup(self, call_id: str) -> bool:
        wc = self.web_calls.get(call_id)
        if wc:
            await wc.hangup("hung up from web UI")
            return True
        for call in list(self.ua.calls.values()):
            if call.id == call_id:
                await call.hangup("hung up from web UI")
                return True
        return False

    # -- camera tools -----------------------------------------------------------------------
    async def probe_camera(self, camera: Camera) -> dict:
        """Probe mic and speaker of a (possibly unsaved) camera via temporary streams."""
        if camera.id in self.busy:
            # probing opens a second talk session, which could cut the live call's audio
            raise CameraBusy("camera is in a call - try again after the call")
        tmp = f"c2s_probe_{secrets.token_hex(3)}"
        result: dict = {"mic": None, "speaker": None, "video": None, "errors": []}
        try:
            mic = camera.mic_source()
            if mic:
                await self.go2rtc.put_stream(tmp, [mic])
                try:
                    s = summarize_probe(await self.go2rtc.probe(tmp, microphone=False))
                    result.update(mic=s["mic"], video=s["video"])
                except Exception as e:
                    result["errors"].append(f"microphone stream: {e}")
            else:
                result["errors"].append("no microphone source configured")
            talk = camera.talk_source()
            if talk:
                await self.go2rtc.put_stream(tmp + "_t", [talk])
                try:
                    s = summarize_probe(await self.go2rtc.probe(tmp + "_t", microphone=True))
                    result["speaker"] = s["speaker"]
                    if not s["speaker"]:
                        result["errors"].append("camera offers no speaker (backchannel) on the talk source")
                except Exception as e:
                    result["errors"].append(f"speaker: {e}")
            elif camera.kind == "tapo":
                result["errors"].append("Tapo speaker needs the TP-Link cloud password")
        finally:
            await self.go2rtc.delete_stream(tmp)
            await self.go2rtc.delete_stream(tmp + "_t")
        if self.store.camera(camera.id) == camera:
            self.probes[camera.id] = dict(result, at=time.time())
        return result

    def probe_later(self, camera: Camera) -> None:
        """Refresh the cached capabilities of a saved camera in the background."""
        async def run() -> None:
            try:
                if camera.id not in self.busy:
                    await self.probe_camera(camera)
            except Exception as e:
                log.debug("background probe of %s failed: %s", camera.name, e)
        asyncio.create_task(run())

    async def snapshot(self, camera: Camera) -> bytes:
        return await self.go2rtc.snapshot(camera.stream_name)

    async def test_notice(self, camera: Camera, text: str, sound: str, voice: str, speed: int) -> dict:
        pcm = await self.prompt_pcm(text, sound, voice, speed)
        return await self.test_speaker(camera, pcm=pcm)

    async def test_speaker(self, camera: Camera, seconds: float = 2.0, pcm=None) -> dict:
        if camera.id in self.busy:
            raise CameraBusy("camera is in a call")
        if not camera.talk_source():
            raise ValueError("camera has no speaker (talk) source configured")
        talk = self.talk_server.create()
        try:
            await self.go2rtc.play(camera.talk_stream_name, talk.url)
            await asyncio.wait_for(talk.connected.wait(), 10)
            codec = talk.codec or "PCMA"
            if pcm is not None:
                chime = g711.encode_pcm(pcm, codec)
            else:
                chime = (g711.tone(codec, 660, 0.35, -9) + g711.silence(codec, 400)
                         + g711.tone(codec, 880, 0.5, -9))
            loop = asyncio.get_running_loop()
            t = loop.time()
            for i in range(0, len(chime), 160):
                talk.push(chime[i:i + 160])
                t += 0.02
                await asyncio.sleep(max(0.0, t - loop.time()))
            await asyncio.sleep(0.5)
            return {"ok": True, "codec": codec, "packets": talk.tx_packets}
        finally:
            talk.close()
            await self.go2rtc.stop_play(camera.talk_stream_name)

    async def record_mic(self, camera: Camera, seconds: float = 4.0) -> bytes:
        buf = bytearray()
        state = {"codec": "PCMA"}

        def on_audio(codec: str, payload: bytes) -> None:
            state["codec"] = codec
            buf.extend(payload)
        client = RtspAudioClient(self.go2rtc.rtsp_url(camera.stream_name, mic_query(camera)), on_audio)
        task = asyncio.create_task(client.run(reconnect=False))
        try:
            await asyncio.wait_for(client.connected.wait(), 10)
            await asyncio.sleep(seconds)
        except TimeoutError:
            raise RuntimeError(client.error or "camera microphone did not connect")
        finally:
            task.cancel()
        return g711.to_wav(bytes(buf), state["codec"])

    # -- status ----------------------------------------------------------------------------
    def status(self) -> dict:
        calls = []
        for call in self.ua.calls.values():
            info = call.info()
            meta = self.call_meta.get(call.id, {})
            session = self.sessions.get(call.id)
            info["bridge"] = meta["bridge"].name or meta["bridge"].id if meta.get("bridge") else None
            info["bridge_id"] = meta["bridge"].id if meta.get("bridge") else None
            if session and session.camera:
                info["camera"] = session.camera.name
            elif session and session.phase == "menu":
                info["camera"] = "IVR menu"
            else:
                info["camera"] = None
            info["phone"] = call.account.cfg.username
            if session:
                info["media"] = session.info()
            calls.append(info)
        for wc in self.web_calls.values():
            info = wc.info()
            info["camera"] = wc.camera.name
            info["bridge"] = None
            info["phone"] = "browser"
            calls.append(info)
        return {
            "version": self.settings.version,
            "uptime": int(time.time() - self.started),
            "go2rtc": {"online": self.go2rtc.online, "version": self.go2rtc.version},
            "sip_port": self.settings.sip_port,
            "phones": {aid: acc.status() for aid, acc in self.ua.accounts.items()},
            "calls": calls,
            "busy_cameras": list(self.busy.keys()),
        }
