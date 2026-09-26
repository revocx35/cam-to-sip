"""Persistent configuration models (stored as JSON in the data directory)."""

from __future__ import annotations

import secrets
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator

SECRET_FIELDS = {"password", "cloud_password"}


def new_id() -> str:
    return secrets.token_hex(4)


class Camera(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    kind: Literal["tapo", "onvif", "custom"] = "tapo"
    enabled: bool = True
    host: str = ""
    rtsp_port: int = 554
    onvif_port: int = 2020
    username: str = ""              # camera account (RTSP / ONVIF)
    password: str = ""
    stream_path: str = "stream2"    # tapo: stream1 (HD) / stream2 (SD); onvif: full RTSP path
    cloud_password: str = ""        # tapo: TP-Link cloud password (talk-back)
    listen_url: str = ""            # custom: any go2rtc source for the microphone
    talk_url: str = ""              # custom: any go2rtc source with a backchannel
    mic_with_video: bool = True     # request video with audio (Tapo only sends audio then)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is required")
        return v

    def _auth(self) -> str:
        if not self.username:
            return ""
        return f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"

    def mic_source(self) -> str:
        if self.kind == "custom":
            return self.listen_url.strip()
        path = self.stream_path.strip().lstrip("/") or "stream2"
        return f"rtsp://{self._auth()}{self.host}:{self.rtsp_port}/{path}"

    def talk_source(self) -> str:
        if self.kind == "tapo":
            if not self.cloud_password:
                return ""
            return f"tapo://{quote(self.cloud_password, safe='')}@{self.host}?subtype=1"
        if self.kind == "onvif":
            return self.mic_source()   # go2rtc asks for the ONVIF backchannel itself
        return self.talk_url.strip()

    @property
    def stream_name(self) -> str:
        return f"c2s_{self.id}"

    @property
    def talk_stream_name(self) -> str:
        return f"c2s_{self.id}_talk"


class Phone(BaseModel):
    """A virtual SIP phone registered to a PBX."""
    id: str = Field(default_factory=new_id)
    name: str
    enabled: bool = True
    server: str
    port: int = 5060
    username: str                    # extension
    password: str = ""
    auth_username: str = ""          # defaults to username
    domain: str = ""                 # defaults to server
    display_name: str = ""
    expires: int = Field(default=300, ge=60, le=3600)
    contact_user: str = Field(default_factory=lambda: "c2s-" + secrets.token_hex(4))

    @field_validator("name", "server", "username")
    @classmethod
    def _required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("required")
        return v


class DtmfAction(BaseModel):
    digit: str
    method: Literal["GET", "POST"] = "POST"
    url: str
    body: str = ""
    hangup: bool = False


class Bridge(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str = ""
    enabled: bool = True
    camera_id: str
    phone_id: str
    answer_delay: float = Field(default=0.0, ge=0, le=60)
    mic_gain_db: float = Field(default=0.0, ge=-20, le=30)       # camera -> phone
    speaker_gain_db: float = Field(default=0.0, ge=-20, le=30)   # phone -> camera
    speaker_gate_db: float | None = Field(default=-50.0, ge=-90, le=0)
    max_call_seconds: int = Field(default=600, ge=10, le=86400)
    allowed_callers: list[str] = Field(default_factory=list)
    hangup_digit: str = ""
    dtmf_actions: list[DtmfAction] = Field(default_factory=list)


class Settings(BaseModel):
    admin_password_hash: str = ""
    session_secret: str = Field(default_factory=lambda: secrets.token_hex(32))
    api_token: str = Field(default_factory=lambda: secrets.token_urlsafe(24))


class Config(BaseModel):
    version: int = 1
    cameras: list[Camera] = Field(default_factory=list)
    phones: list[Phone] = Field(default_factory=list)
    bridges: list[Bridge] = Field(default_factory=list)
    settings: Settings = Field(default_factory=Settings)


def redact(model: BaseModel) -> dict:
    """Dump a model for the API without secrets (adds `<field>_set` flags)."""
    data = model.model_dump()
    for f in SECRET_FIELDS:
        if f in data:
            data[f + "_set"] = bool(data[f])
            data[f] = ""
    return data
