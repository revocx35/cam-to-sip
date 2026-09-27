"""Process settings from environment variables (all prefixed CAM2SIP_)."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field

from . import __version__


def _env(name: str, default: str) -> str:
    return os.environ.get(f"CAM2SIP_{name}", default).strip()


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


# Same set as Caddy's `private_ranges`: loopback and private networks.
PRIVATE_RANGES = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7"]


def parse_trusted_proxies(value: str) -> list[str]:
    """CAM2SIP_TRUSTED_PROXIES: comma-separated IPs/networks, `private` or `none`.

    Only these peers may set X-Forwarded-For/-Proto. The client address is the right-most
    X-Forwarded-For entry that is not a trusted proxy, so a client can't spoof it by sending
    its own header. `*` is refused: it makes the left-most, client-controlled entry count.
    """
    out: list[str] = []
    for item in (x.strip() for x in value.split(",")):
        if not item or item.lower() == "none":
            continue
        if item.lower() == "private":
            out += PRIVATE_RANGES
            continue
        try:
            out.append(str(ipaddress.ip_network(item, strict=False)))
        except ValueError:
            raise ValueError(f"CAM2SIP_TRUSTED_PROXIES: {item!r} is not an IP address or network "
                             "(use addresses/CIDRs, 'private' or 'none')") from None
    return out


def _cookie_mode(value: str) -> str:
    v = value.lower()
    if v in ("1", "true", "yes"):
        return "true"
    if v in ("0", "false", "no"):
        return "false"
    return "auto"


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
    # auto: Secure when a trusted reverse proxy reports HTTPS (X-Forwarded-Proto); true / false: always / never
    secure_cookies: str = field(default_factory=lambda: _cookie_mode(_env("SECURE_COOKIES", "auto")))
    trusted_proxies: list[str] = field(default_factory=lambda: parse_trusted_proxies(_env("TRUSTED_PROXIES", "private")))
