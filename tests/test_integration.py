"""Integration API v1 (telerouter): token auth, camera targets, one-time call addresses."""

import asyncio
import socket

import pytest
from fastapi.testclient import TestClient

from cam2sip import integration
from cam2sip.engine import Engine
from cam2sip.media.rtp import PortAllocator
from cam2sip.models import Camera
from cam2sip.settings import Settings
from cam2sip.sip.ua import Account, AccountConfig, UserAgent
from cam2sip.store import Store
from cam2sip.web.app import create_app

from .conftest import free_port


@pytest.fixture()
def client(tmp_path):
    s = Settings(data_dir=str(tmp_path), sip_port=free_port(), talk_port=free_port(), https_port=0,
                 go2rtc_api="http://127.0.0.1:9", rtp_port_min=31300, rtp_port_max=31319)
    with TestClient(create_app(s), base_url="https://testserver") as c:
        assert c.post("/api/setup", json={"password": "secret1234"}).status_code == 200
        c.post("/api/cameras", json={"name": "Front door", "kind": "tapo", "host": "10.0.0.5"})
        yield c


def test_token_auth_targets_and_tickets(client, tmp_path):
    tok = client.post("/api/settings/integration-token").json()["token"]
    assert tok.startswith("c2s_")
    st = client.app.state.store.config.settings
    assert st.integration_token_hash == integration.token_hash(tok) and tok not in st.model_dump_json()
    h = {"Authorization": f"Bearer {tok}"}
    client.post("/api/logout")
    assert client.get("/api/integration/v1/targets").status_code == 401
    assert client.get("/api/integration/v1/targets", headers={"Authorization": "Bearer c2s_nope"}).status_code == 401
    assert client.get("/api/cameras", headers=h).status_code == 401          # the token reaches nothing else
    targets = client.get("/api/integration/v1/targets", headers=h).json()["targets"]
    assert [t["name"] for t in targets] == ["Front door"] and targets[0]["kind"] == "camera"
    r = client.post("/api/integration/v1/calls", headers=h, json={"target": targets[0]["id"]}).json()
    assert r["sip_uri"].startswith("sip:tr") and r["expires_in"] == 30
    assert client.post("/api/integration/v1/calls", headers=h, json={"target": "nope"}).status_code == 404
    s = Settings(data_dir=str(tmp_path), sip_port=free_port(), talk_port=free_port(), https_port=0,
                 go2rtc_api="http://127.0.0.1:9", rtp_port_min=31320, rtp_port_max=31339)
    with TestClient(create_app(s), base_url="http://testserver") as plain:
        assert plain.get("/api/integration/v1/info", headers=h).status_code == 403   # HTTPS only


@pytest.mark.asyncio
async def test_ticket_call_connects_the_camera_once(tmp_path):
    settings = Settings(data_dir=str(tmp_path), sip_port=free_port(), talk_port=free_port(), https_port=0,
                        go2rtc_api="http://127.0.0.1:9", advertise_ip="127.0.0.1",
                        rtp_port_min=31340, rtp_port_max=31359)
    store = Store(tmp_path)
    cam = Camera(name="Front door", host="10.0.0.1")
    store.config.cameras.append(cam)
    engine = Engine(settings, store)
    await engine.start()
    ua = UserAgent(free_port(), PortAllocator(31360, 31379), advertise_ip="127.0.0.1")
    await ua.start()
    acc = Account(ua, AccountConfig(id="t", server="127.0.0.1", port=settings.sip_port, username="telerouter",
                                    password="", contact_user="telerouter"))
    try:
        t = engine.tickets.create(cam.id, "127.0.0.1")
        call = await acc.dial(f"{t.user}@127.0.0.1:{settings.sip_port}")
        await asyncio.wait_for(call.answered.wait(), 5)
        await asyncio.sleep(0.3)
        assert engine.busy.get(cam.id) is not None
        session = next(iter(engine.sessions.values()))
        assert session.phase == "connected" and session.camera.id == cam.id
        # the same address doesn't work twice, and the camera is busy meanwhile
        again = await acc.dial(f"{t.user}@127.0.0.1:{settings.sip_port}")
        await asyncio.wait_for(again.ended.wait(), 5)
        assert "404" in again.end_reason
        await call.hangup()
        await asyncio.sleep(0.5)
        assert not engine.busy and not engine.sessions
        # a ticket bound to another IP is refused
        t2 = engine.tickets.create(cam.id, "10.1.1.1")
        other = await acc.dial(f"{t2.user}@127.0.0.1:{settings.sip_port}")
        await asyncio.wait_for(other.ended.wait(), 5)
        assert "404" in other.end_reason
    finally:
        await ua.stop()
        await engine.stop()
