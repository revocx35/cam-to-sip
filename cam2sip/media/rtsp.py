"""Tiny RTSP pieces used to exchange audio with go2rtc over localhost.

* ``RtspAudioClient`` pulls the camera microphone from go2rtc's RTSP server
  (DESCRIBE/SETUP/PLAY, RTP interleaved over TCP).
* ``TalkServer`` is an RTSP *server* that go2rtc pulls phone audio from when
  we ask it to play a source into the camera's backchannel
  (``POST /api/streams?dst=<talk stream>&src=rtsp://127.0.0.1:<port>/talk/<token>``).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import struct
from collections.abc import Callable
from urllib.parse import urlsplit

from ..sip.sdp import STATIC_PT, Sdp
from .rtp import RtpPacket, RtpSender

log = logging.getLogger("cam2sip.rtsp")

G711 = {"PCMA": 8, "PCMU": 0}


async def _read_message(reader: asyncio.StreamReader) -> tuple[str, dict[str, str], bytes] | None:
    """Read one RTSP request/response. Returns None for an interleaved frame."""
    first = await reader.readexactly(1)
    while first in (b"\r", b"\n"):  # tolerate stray CRLFs
        first = await reader.readexactly(1)
    if first == b"$":  # interleaved binary frame (e.g. RTCP) - skip it
        hdr = await reader.readexactly(3)
        await reader.readexactly(struct.unpack("!H", hdr[1:])[0])
        return None
    start = (first + await reader.readline()).decode("utf-8", "replace").strip()
    headers: dict[str, str] = {}
    while True:
        line = (await reader.readline()).decode("utf-8", "replace")
        if line == "":
            raise ConnectionError("connection closed")
        if line in ("\r\n", "\n"):
            break
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    body = b""
    n = int(headers.get("content-length", "0") or 0)
    if n:
        body = await reader.readexactly(n)
    return start, headers, body


class RtspAudioClient:
    """Receive one audio track from an RTSP URL (G.711 only)."""

    def __init__(self, url: str, on_audio: Callable[[str, bytes], None]):
        self.url = url
        self.on_audio = on_audio
        self.codec: str | None = None
        self.connected = asyncio.Event()
        self.error: str | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._cseq = 0
        self._session: str | None = None

    async def run(self, reconnect: bool = True) -> None:
        backoff = 1.0
        while True:
            try:
                await self._run_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network / protocol problems
                self.error = str(e) or e.__class__.__name__
                log.warning("mic stream %s: %s", _redact(self.url), self.error)
            finally:
                self.connected.clear()
                self._close()
            if not reconnect:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    def _close(self) -> None:
        if self._writer:
            self._writer.close()
            self._writer = None

    async def _request(self, reader, method: str, url: str, extra: dict[str, str] | None = None):
        self._cseq += 1
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self._cseq}", "User-Agent: cam2sip"]
        if self._session:
            lines.append(f"Session: {self._session}")
        for k, v in (extra or {}).items():
            lines.append(f"{k}: {v}")
        self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await self._writer.drain()
        msg = None
        while msg is None:
            msg = await asyncio.wait_for(_read_message(reader), 10)
        start, headers, body = msg
        parts = start.split(" ", 2)
        if len(parts) < 2 or parts[1] != "200":
            raise ConnectionError(f"{method} failed: {start}")
        return headers, body

    async def _run_once(self) -> None:
        u = urlsplit(self.url)
        reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(u.hostname, u.port or 554), 5)
        self._cseq, self._session = 0, None
        headers, body = await self._request(reader, "DESCRIBE", self.url, {"Accept": "application/sdp"})
        base = headers.get("content-base") or self.url
        sdp = Sdp.parse(body)
        track = None
        for m in sdp.media:
            if m.kind != "audio":
                continue
            for f in m.fmts:
                if f.isdigit() and m.codec_name(int(f)) in G711:
                    track = (m, int(f), m.codec_name(int(f)))
                    break
            if track:
                break
        if not track:
            raise ConnectionError("camera audio is not G.711 (PCMA/PCMU)")
        media, pt, codec = track
        control = next((a[8:] for a in media.attrs if a.startswith("control:")), "")
        if control.startswith("rtsp://"):
            setup_url = control
        elif control and control != "*":
            setup_url = base.split("?")[0].rstrip("/") + "/" + control
        else:
            setup_url = base
        headers, _ = await self._request(reader, "SETUP", setup_url,
                                         {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
        self._session = (headers.get("session") or "").split(";")[0] or None
        channel = 0
        transport = headers.get("transport", "")
        if "interleaved=" in transport:
            channel = int(transport.split("interleaved=")[1].split("-")[0].split(";")[0])
        await self._request(reader, "PLAY", base, {"Range": "npt=0.000-"})
        self.codec = codec
        self.error = None
        self.connected.set()
        log.info("mic stream connected (%s) %s", codec, _redact(self.url))
        keepalive = asyncio.create_task(self._keepalive())
        try:
            while True:
                b = await asyncio.wait_for(reader.readexactly(1), 15)
                if b == b"$":
                    hdr = await reader.readexactly(3)
                    length = struct.unpack("!H", hdr[1:])[0]
                    data = await reader.readexactly(length)
                    if hdr[0] != channel:
                        continue
                    pkt = RtpPacket.parse(data)
                    if pkt and pkt.payload and STATIC_PT.get(pkt.pt, codec) in G711:
                        self.on_audio(STATIC_PT.get(pkt.pt, codec), pkt.payload)
                else:
                    # an RTSP response (keepalive answer) - consume it
                    rest = await reader.readuntil(b"\r\n\r\n")
                    head = (b + rest).decode("utf-8", "replace")
                    for line in head.split("\r\n"):
                        if line.lower().startswith("content-length:"):
                            await reader.readexactly(int(line.split(":")[1]))
        finally:
            keepalive.cancel()

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(20)
            if not self._writer:
                return
            self._cseq += 1
            msg = f"OPTIONS {self.url} RTSP/1.0\r\nCSeq: {self._cseq}\r\n"
            if self._session:
                msg += f"Session: {self._session}\r\n"
            self._writer.write((msg + "\r\n").encode())


class TalkSession:
    """Audio queued for one camera speaker. go2rtc connects and pulls it."""

    def __init__(self, server: "TalkServer", token: str):
        self.server = server
        self.token = token
        self.codec: str | None = None
        self.connected = asyncio.Event()
        self.closed = False
        self._writer: asyncio.StreamWriter | None = None
        self._channel = 0
        self._sender = RtpSender()
        self.tx_packets = 0

    @property
    def url(self) -> str:
        return f"rtsp://127.0.0.1:{self.server.port}/talk/{self.token}"

    def push(self, payload: bytes) -> None:
        """Send one frame (already in ``self.codec``) to go2rtc."""
        w = self._writer
        if not w or w.is_closing() or not self.codec:
            return
        rtp = self._sender.packet(G711[self.codec], payload, len(payload))
        w.write(b"$" + bytes([self._channel]) + struct.pack("!H", len(rtp)) + rtp)
        self.tx_packets += 1

    def advance(self, samples: int) -> None:
        """Skip timestamps for a pause, so the camera sees real elapsed time."""
        if samples > 0:
            self._sender.ts = (self._sender.ts + samples) & 0xFFFFFFFF

    def close(self) -> None:
        self.closed = True
        self.server.sessions.pop(self.token, None)
        if self._writer:
            self._writer.close()
            self._writer = None
        self.connected.clear()


class TalkServer:
    """Minimal RTSP server offering per-call talk streams (PCMA + PCMU tracks)."""

    TRACKS = {"trackID=0": "PCMA", "trackID=1": "PCMU"}

    def __init__(self, host: str = "127.0.0.1", port: int = 18555):
        self.host = host
        self.port = port
        self.sessions: dict[str, TalkSession] = {}
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("talk RTSP server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for s in list(self.sessions.values()):
            s.close()

    def create(self) -> TalkSession:
        s = TalkSession(self, secrets.token_hex(8))
        self.sessions[s.token] = s
        return s

    def _lookup(self, url: str) -> tuple[TalkSession | None, str]:
        path = urlsplit(url).path.strip("/")
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "talk":
            return self.sessions.get(parts[1]), "/".join(parts[2:])
        return None, ""

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session: TalkSession | None = None
        sid = secrets.token_hex(6)
        try:
            while True:
                try:
                    msg = await _read_message(reader)
                except (ConnectionError, asyncio.IncompleteReadError):
                    break
                if msg is None:
                    continue
                start, headers, _ = msg
                method, _, rest = start.partition(" ")
                url = rest.rsplit(" ", 1)[0]
                cseq = headers.get("cseq", "0")
                extra: dict[str, str] = {}
                body = b""
                status = "200 OK"
                if method == "OPTIONS":
                    extra["Public"] = "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER"
                elif method == "DESCRIBE":
                    session, _ = self._lookup(url)
                    if not session:
                        status = "404 Not Found"
                    else:
                        body = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=cam2sip talk\r\n"
                                "c=IN IP4 0.0.0.0\r\nt=0 0\r\n"
                                "m=audio 0 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\na=control:trackID=0\r\n"
                                "m=audio 0 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=control:trackID=1\r\n").encode()
                        extra["Content-Type"] = "application/sdp"
                        extra["Content-Base"] = session.url + "/"
                elif method == "SETUP":
                    session, track = self._lookup(url)
                    transport = headers.get("transport", "")
                    if not session or track not in self.TRACKS:
                        status = "404 Not Found"
                    elif "interleaved=" not in transport:
                        status = "461 Unsupported Transport"
                    else:
                        ch = transport.split("interleaved=")[1].split(";")[0]
                        session._channel = int(ch.split("-")[0])
                        session.codec = self.TRACKS[track]
                        extra["Transport"] = f"RTP/AVP/TCP;unicast;interleaved={ch}"
                        extra["Session"] = f"{sid};timeout=60"
                elif method == "PLAY":
                    extra["Session"] = sid
                elif method == "TEARDOWN":
                    extra["Session"] = sid
                elif method in ("GET_PARAMETER", "SET_PARAMETER"):
                    pass
                else:
                    status = "405 Method Not Allowed"
                resp = [f"RTSP/1.0 {status}", f"CSeq: {cseq}", "Server: cam2sip"]
                resp += [f"{k}: {v}" for k, v in extra.items()]
                resp.append(f"Content-Length: {len(body)}")
                writer.write(("\r\n".join(resp) + "\r\n\r\n").encode() + body)
                await writer.drain()
                if method == "PLAY" and session and session.codec and not session.closed:
                    session._writer = writer
                    session.connected.set()
                    log.info("camera speaker stream attached (%s)", session.codec)
                if method == "TEARDOWN":
                    break
        finally:
            if session and session._writer is writer:
                session._writer = None
                session.connected.clear()
            writer.close()


def _redact(url: str) -> str:
    u = urlsplit(url)
    if u.password:
        return url.replace(f":{u.password}@", ":***@")
    return url
