"""Just enough ONVIF to discover a camera's RTSP streams and audio outputs.

Uses plain SOAP 1.2 with a WS-Security UsernameToken (password digest),
compensating for camera clock skew via GetSystemDateAndTime.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urlunsplit

import httpx

NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
NS_SCHEMA = "http://www.onvif.org/ver10/schema"


class OnvifError(Exception):
    pass


def _find(el: ET.Element | None, path: str) -> ET.Element | None:
    if el is None:
        return None
    return el.find(".//{*}" + path.replace("/", "/{*}"))


def _text(el: ET.Element | None, path: str, default: str = "") -> str:
    found = _find(el, path)
    return (found.text or "").strip() if found is not None and found.text else default


class OnvifClient:
    def __init__(self, host: str, port: int, username: str, password: str, timeout: float = 8):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.device_url = f"http://{host}:{port}/onvif/device_service"
        self.media_url: str | None = None
        self.offset = dt.timedelta(0)
        self.http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.http.aclose()

    def _security(self) -> str:
        if not self.username:
            return ""
        nonce = os.urandom(16)
        created = (dt.datetime.now(dt.timezone.utc) + self.offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()).decode()
        return (
            '<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            f"<UsernameToken><Username>{_esc(self.username)}</Username>"
            '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
            f"{digest}</Password>"
            '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-soap-message-security-1.0#Base64Binary">'
            f"{base64.b64encode(nonce).decode()}</Nonce>"
            f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
            "</UsernameToken></Security>"
        )

    async def _call(self, url: str, body: str, auth: bool = True) -> ET.Element:
        env = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            f"<s:Header>{self._security() if auth else ''}</s:Header>"
            f"<s:Body>{body}</s:Body></s:Envelope>"
        )
        try:
            r = await self.http.post(url, content=env.encode(),
                                     headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        except httpx.HTTPError as e:
            raise OnvifError(f"cannot reach {url}: {e}") from e
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            raise OnvifError(f"invalid ONVIF response (HTTP {r.status_code})") from e
        fault = _find(root, "Fault")
        if fault is not None:
            reason = _text(fault, "Text") or "SOAP fault"
            if "not authorized" in reason.lower() or r.status_code in (400, 401):
                raise OnvifError(f"authentication failed: {reason}")
            raise OnvifError(reason)
        return root

    def _fix_host(self, url: str) -> str:
        """Cameras often report an internal/wrong host in XAddrs - keep ours."""
        u = urlsplit(url)
        port = u.port or (self.port if u.scheme == "http" else None)
        netloc = f"{self.host}:{port}" if port else self.host
        return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))

    async def sync_time(self) -> None:
        try:
            root = await self._call(self.device_url, f'<GetSystemDateAndTime xmlns="{NS_DEVICE}"/>', auth=False)
        except OnvifError:
            return
        utc = _find(root, "UTCDateTime")
        if utc is None:
            return
        try:
            cam = dt.datetime(
                int(_text(utc, "Date/Year")), int(_text(utc, "Date/Month")), int(_text(utc, "Date/Day")),
                int(_text(utc, "Time/Hour")), int(_text(utc, "Time/Minute")), int(_text(utc, "Time/Second")),
                tzinfo=dt.timezone.utc)
            self.offset = cam - dt.datetime.now(dt.timezone.utc)
        except ValueError:
            pass

    async def device_info(self) -> dict:
        root = await self._call(self.device_url, f'<GetDeviceInformation xmlns="{NS_DEVICE}"/>')
        return {k: _text(root, k) for k in ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber")}

    async def media_service(self) -> str:
        if self.media_url:
            return self.media_url
        root = await self._call(self.device_url,
                                f'<GetCapabilities xmlns="{NS_DEVICE}"><Category>Media</Category></GetCapabilities>')
        xaddr = _text(root, "Media/XAddr")
        self.media_url = self._fix_host(xaddr) if xaddr else self.device_url
        return self.media_url

    async def profiles(self) -> list[dict]:
        url = await self.media_service()
        root = await self._call(url, f'<GetProfiles xmlns="{NS_MEDIA}"/>')
        out = []
        for p in root.iter():
            if not p.tag.endswith("}Profiles"):
                continue
            token = p.get("token", "")
            venc = _find(p, "VideoEncoderConfiguration")
            aenc = _find(p, "AudioEncoderConfiguration")
            out.append({
                "token": token,
                "name": _text(p, "Name"),
                "video": {
                    "encoding": _text(venc, "Encoding"),
                    "width": _text(venc, "Resolution/Width"),
                    "height": _text(venc, "Resolution/Height"),
                } if venc is not None else None,
                "audio": {"encoding": _text(aenc, "Encoding")} if aenc is not None else None,
                "audio_output": _find(p, "AudioOutputConfiguration") is not None,
            })
        return out

    async def stream_uri(self, token: str) -> str:
        url = await self.media_service()
        body = (
            f'<GetStreamUri xmlns="{NS_MEDIA}"><StreamSetup>'
            f'<Stream xmlns="{NS_SCHEMA}">RTP-Unicast</Stream>'
            f'<Transport xmlns="{NS_SCHEMA}"><Protocol>RTSP</Protocol></Transport>'
            f"</StreamSetup><ProfileToken>{_esc(token)}</ProfileToken></GetStreamUri>"
        )
        root = await self._call(url, body)
        return _text(root, "Uri")

    async def audio_outputs(self) -> int:
        url = await self.media_service()
        try:
            root = await self._call(url, f'<GetAudioOutputs xmlns="{NS_MEDIA}"/>')
        except OnvifError:
            return 0
        return sum(1 for el in root.iter() if el.tag.endswith("}AudioOutputs"))


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def discover(host: str, port: int, username: str, password: str) -> dict:
    """Return device info, profiles with RTSP URIs and speaker presence."""
    c = OnvifClient(host, port, username, password)
    try:
        await c.sync_time()
        info = await c.device_info()
        profiles = await c.profiles()
        for p in profiles:
            try:
                p["rtsp"] = await c.stream_uri(p["token"])
            except OnvifError as e:
                p["rtsp"] = ""
                p["error"] = str(e)
        outputs = await c.audio_outputs()
        return {"device": info, "profiles": profiles, "audio_outputs": outputs}
    finally:
        await c.close()
