import asyncio

import pytest
from fastapi.testclient import TestClient

from cam2sip.engine import Engine
from cam2sip.media.rtp import PortAllocator
from cam2sip.models import Bridge, Camera, Phone
from cam2sip.settings import Settings
from cam2sip.sip.ua import Account, AccountConfig, UserAgent
from cam2sip.store import Store
from cam2sip.web.app import create_app

from .conftest import free_port


def test_route_bridge_and_conflicts(tmp_path):
    store = Store(tmp_path)
    ivr = Bridge(name="menu", phone_id="p", mode="ivr", allowed_callers=[" 1005", "1005", ""],
                 ivr_options=[{"digit": "1", "camera_id": "a"}])
    direct = Bridge(name="door", phone_id="p", camera_id="a", allowed_callers=["1006"])
    store.config.bridges += [ivr, direct]
    assert ivr.allowed_callers == ["1005"]                    # trimmed + de-duplicated
    assert store.route_bridge("p", "1005")[0] is ivr
    assert store.route_bridge("p", "1006")[0] is direct
    assert store.route_bridge("p", "1007") == (None, "caller not allowed")
    default = Bridge(name="default", phone_id="p", camera_id="a")
    assert store.routing_conflict(default) is None
    store.config.bridges.append(default)
    assert store.route_bridge("p", "1007")[0] is default and store.route_bridge("p", "1005")[0] is ivr
    # a second default and an overlapping caller are refused; disabled bridges don't count
    assert "default bridge" in store.routing_conflict(Bridge(phone_id="p", camera_id="a"))
    assert "1006" in store.routing_conflict(Bridge(phone_id="p", camera_id="a", allowed_callers=["1006", "1009"]))
    assert store.routing_conflict(Bridge(phone_id="p", camera_id="a", enabled=False)) is None
    assert store.routing_conflict(Bridge(phone_id="other", camera_id="a")) is None
    direct.enabled = False
    assert store.route_bridge("p", "1006")[0] is default       # disabled bridge -> falls to default
    assert store.route_bridge("q", "1005") == (None, "no enabled bridge")


def test_bridge_api_conflicts(tmp_path):
    s = Settings(data_dir=str(tmp_path), sip_port=free_port(), talk_port=free_port(), https_port=0,
                 go2rtc_api="http://127.0.0.1:9", rtp_port_min=31500, rtp_port_max=31519)
    with TestClient(create_app(s)) as c:
        c.post("/api/setup", json={"password": "secret1234"})
        cam = c.post("/api/cameras", json={"name": "Door", "host": "h"}).json()
        ph = c.post("/api/phones", json={"name": "P", "server": "127.0.0.1", "port": 9, "username": "1008"}).json()
        ivr = c.post("/api/bridges", json={"phone_id": ph["id"], "mode": "ivr", "allowed_callers": ["1005"],
                                           "ivr_options": [{"digit": "1", "camera_id": cam["id"]}]})
        direct = c.post("/api/bridges", json={"phone_id": ph["id"], "camera_id": cam["id"], "allowed_callers": ["1006"]})
        assert ivr.status_code == 200 and direct.status_code == 200
        dup = c.post("/api/bridges", json={"phone_id": ph["id"], "camera_id": cam["id"], "allowed_callers": ["1005"]})
        assert dup.status_code == 409 and "1005" in dup.json()["detail"]
        assert c.post("/api/bridges", json={"phone_id": ph["id"], "camera_id": cam["id"]}).status_code == 200
        second_default = c.post("/api/bridges", json={"phone_id": ph["id"], "camera_id": cam["id"]})
        assert second_default.status_code == 409 and "default bridge" in second_default.json()["detail"]
        # editing a bridge doesn't conflict with itself
        assert c.put(f"/api/bridges/{direct.json()['id']}", json={"allowed_callers": ["1006", "1010"]}).status_code == 200


@pytest.mark.asyncio
async def test_calls_routed_by_caller(tmp_path):
    settings = Settings(data_dir=str(tmp_path), sip_port=free_port(), talk_port=free_port(), https_port=0,
                        go2rtc_api="http://127.0.0.1:9", advertise_ip="127.0.0.1",
                        rtp_port_min=31520, rtp_port_max=31539)
    store = Store(tmp_path)
    cam_a, cam_b = Camera(name="Front", host="10.0.0.1"), Camera(name="Garage", host="10.0.0.2")
    phone = Phone(name="Intercom", server="127.0.0.1", port=9, username="1008")
    store.config.cameras += [cam_a, cam_b]
    store.config.phones.append(phone)
    store.config.bridges += [
        Bridge(name="menu", phone_id=phone.id, mode="ivr", ivr_voice="en-us", allowed_callers=["1005"],
               ivr_options=[{"digit": "1", "camera_id": cam_a.id}, {"digit": "2", "camera_id": cam_b.id}]),
        Bridge(name="garage direct", phone_id=phone.id, camera_id=cam_b.id, allowed_callers=["1006"]),
    ]
    engine = Engine(settings, store)
    await engine.start()
    ua = UserAgent(free_port(), PortAllocator(31540, 31559), advertise_ip="127.0.0.1")
    await ua.start()

    async def call_from(ext: str):
        acc = Account(ua, AccountConfig(id=ext, server="127.0.0.1", port=settings.sip_port, username=ext,
                                        password="x", contact_user=f"u{ext}"))
        ua.accounts[ext] = acc
        call = await acc.dial(f"{phone.contact_user}@127.0.0.1:{settings.sip_port}")
        await asyncio.wait({asyncio.create_task(call.answered.wait()), asyncio.create_task(call.ended.wait())},
                           timeout=5, return_when=asyncio.FIRST_COMPLETED)
        await asyncio.sleep(0.3)
        return call

    try:
        c1005 = await call_from("1005")
        s1005 = next(s for s in engine.sessions.values() if s.call.remote_user == "1005")
        assert c1005.state == "active" and s1005.bridge.name == "menu" and s1005.phase == "menu"
        c1006 = await call_from("1006")
        s1006 = next(s for s in engine.sessions.values() if s.call.remote_user == "1006")
        assert c1006.state == "active" and s1006.bridge.name == "garage direct"
        assert s1006.phase == "connected" and s1006.camera.id == cam_b.id
        c1007 = await call_from("1007")
        assert c1007.state == "ended" and c1007.end_reason.startswith("403")
        # both calls run at the same time on the same virtual phone
        assert {s.bridge.name for s in engine.sessions.values()} == {"menu", "garage direct"}
        await c1005.hangup()
        await c1006.hangup()
        await asyncio.sleep(0.3)
        assert [h["bridge"] for h in store.history[-3:]] == [None, "menu", "garage direct"]
    finally:
        await ua.stop()
        await engine.stop()
