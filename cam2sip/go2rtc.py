"""go2rtc HTTP API client and stream reconciliation."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlencode

import httpx

log = logging.getLogger("cam2sip.go2rtc")

PREFIX = "c2s_"


class Go2rtc:
    def __init__(self, api_url: str, rtsp_url: str):
        self.api = api_url.rstrip("/")
        self.rtsp = rtsp_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=15)
        self.online = False
        self.version: str | None = None
        self.desired: dict[str, list[str]] = {}
        self._applied: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    def rtsp_url(self, stream: str, query: str = "") -> str:
        return f"{self.rtsp}/{stream}" + (f"?{query}" if query else "")

    async def info(self) -> dict:
        r = await self.client.get(f"{self.api}/api")
        r.raise_for_status()
        return r.json()

    async def streams(self) -> dict:
        r = await self.client.get(f"{self.api}/api/streams")
        r.raise_for_status()
        return r.json() or {}

    async def put_stream(self, name: str, sources: list[str]) -> None:
        params = [("name", name)] + [("src", s) for s in sources]
        r = await self.client.put(f"{self.api}/api/streams?{urlencode(params)}")
        # go2rtc returns 400 "config file disabled" when it runs without a config
        # file - the stream is still created in memory, which is what we want.
        if r.status_code >= 400 and "config file" not in r.text:
            raise RuntimeError(f"go2rtc rejected stream {name}: {r.text.strip()}")

    async def delete_stream(self, name: str) -> None:
        await self.client.delete(f"{self.api}/api/streams", params={"src": name})

    async def play(self, dst: str, src: str) -> None:
        r = await self.client.post(f"{self.api}/api/streams", params={"dst": dst, "src": src})
        if r.status_code >= 400:
            raise RuntimeError(f"go2rtc play failed: {r.text.strip() or r.status_code}")

    async def stop_play(self, dst: str) -> None:
        try:
            await self.client.post(f"{self.api}/api/streams", params={"dst": dst, "src": ""})
        except httpx.HTTPError:
            pass

    async def probe(self, name: str, microphone: bool = True) -> dict:
        """Connect to a stream and report its media (forces the producer to start)."""
        q = "&audio=all&video=all" + ("&microphone" if microphone else "")
        r = await self.client.get(f"{self.api}/api/streams?src={name}{q}", timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(r.text.strip() or f"HTTP {r.status_code}")
        return r.json()

    async def snapshot(self, name: str) -> bytes:
        r = await self.client.get(f"{self.api}/api/frame.jpeg", params={"src": name}, timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(r.text.strip() or f"HTTP {r.status_code}")
        return r.content

    # -- reconciliation ---------------------------------------------------------
    async def sync(self, desired: dict[str, list[str]]) -> None:
        self.desired = desired
        await self.reconcile(force=True)

    async def reconcile(self, force: bool = False) -> None:
        async with self._lock:
            try:
                existing = await self.streams()
                if not self.online:
                    info = await self.info()
                    self.version = info.get("version")
                    log.info("connected to go2rtc %s at %s", self.version or "", self.api)
                self.online = True
            except Exception as e:
                if self.online:
                    log.warning("go2rtc unreachable: %s", e)
                self.online = False
                self._applied = {}
                return
            for name, sources in self.desired.items():
                if force or name not in existing or self._applied.get(name) != sources:
                    if not sources:
                        continue
                    try:
                        await self.put_stream(name, sources)
                        self._applied[name] = sources
                    except Exception as e:
                        log.error("go2rtc: %s", e)
            for name in existing:
                if name.startswith(PREFIX) and name not in self.desired:
                    await self.delete_stream(name)
                    self._applied.pop(name, None)

    async def run(self, interval: float = 15.0) -> None:
        while True:
            await self.reconcile()
            await asyncio.sleep(interval)


def summarize_probe(data: dict) -> dict:
    """Extract mic / speaker capability from a go2rtc probe response."""
    mic = speaker = None
    video = None
    errors = []
    for p in data.get("producers") or []:
        for m in p.get("medias") or []:
            parts = [x.strip() for x in m.split(",")]
            if len(parts) < 3:
                continue
            kind, direction, codecs = parts[0], parts[1], parts[2:]
            if kind == "audio" and direction == "recvonly" and not mic:
                mic = codecs[0]
            elif kind == "audio" and direction == "sendonly" and not speaker:
                speaker = codecs[0]
            elif kind == "video" and direction == "recvonly" and not video:
                video = codecs[0]
        if p.get("error"):
            errors.append(p["error"])
    return {"mic": mic, "speaker": speaker, "video": video, "errors": errors}
