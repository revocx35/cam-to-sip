"""Process settings from environment variables (all prefixed CAM2SIP_)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import __version__


def _env(name: str, default: str) -> str:
    return os.environ.get(f"CAM2SIP_{name}", default).strip()


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


@dataclass
class Settings:
    version: str = __version__
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "/data"))
    web_host: str = field(default_factory=lambda: _env("WEB_HOST", "0.0.0.0"))
    web_port: int = field(default_factory=lambda: _int("WEB_PORT", 8090))
    https_port: int = field(default_factory=lambda: _int("HTTPS_PORT", 8443))   # 0 = off
    tls_cert: str = field(default_factory=lambda: _env("TLS_CERT", ""))
    tls_key: str = field(default_factory=lambda: _env("TLS_KEY", ""))
    sip_bind: str = field(default_factory=lambda: _env("SIP_BIND", "0.0.0.0"))
    sip_port: int = field(default_factory=lambda: _int("SIP_PORT", 5062))
    advertise_ip: str = field(default_factory=lambda: _env("ADVERTISE_IP", ""))
    rtp_port_min: int = field(default_factory=lambda: _int("RTP_PORT_MIN", 16000))
    rtp_port_max: int = field(default_factory=lambda: _int("RTP_PORT_MAX", 16199))
    talk_port: int = field(default_factory=lambda: _int("TALK_PORT", 18555))
    piper_port: int = field(default_factory=lambda: _int("PIPER_PORT", 18556))
    go2rtc_api: str = field(default_factory=lambda: _env("GO2RTC_API", "http://127.0.0.1:11984"))
    go2rtc_rtsp: str = field(default_factory=lambda: _env("GO2RTC_RTSP", "rtsp://127.0.0.1:18554"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", ""))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    sip_trace: bool = field(default_factory=lambda: _env("SIP_TRACE", "false").lower() in ("1", "true", "yes"))
    secure_cookies: bool = field(default_factory=lambda: _env("SECURE_COOKIES", "false").lower() in ("1", "true", "yes"))
