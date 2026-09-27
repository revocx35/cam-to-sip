import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cam2sip.settings import Settings
from cam2sip.web.app import create_app

from .conftest import free_port


@pytest.fixture()
def client(tmp_path):
    s = Settings(data_dir=str(tmp_path), sip_port=free_port(), talk_port=free_port(), https_port=0,
                 go2rtc_api="http://127.0.0.1:9", rtp_port_min=31100, rtp_port_max=31119)
    with TestClient(create_app(s)) as c:
        c.post("/api/setup", json={"password": "secret1234"})
        yield c


def _camera(client):
    return client.post("/api/cameras", json={"name": "Door", "kind": "tapo", "host": "10.0.0.5",
                                             "username": "u", "password": "p", "cloud_password": "c"}).json()


def test_web_call_lifecycle(client):
    cam = _camera(client)
    with client.websocket_connect(f"/api/cameras/{cam['id']}/talk") as ws:
        status = json.loads(ws.receive_text())
        assert status["type"] == "status" and "mic" in status and "speaker" in status
        ws.send_bytes(b"\xd5" * 160)                      # browser audio frame (A-law)
        ws.send_text(json.dumps({"type": "gate", "db": None}))
        st = client.get("/api/status").json()
        assert st["busy_cameras"] == [cam["id"]]
        web = [c for c in st["calls"] if c["direction"] == "web"]
        assert len(web) == 1 and web[0]["camera"] == "Door"

        # a second browser (or a SIP caller) gets "busy"
        with client.websocket_connect(f"/api/cameras/{cam['id']}/talk") as ws2:
            err = json.loads(ws2.receive_text())
            assert err["type"] == "error" and "already in a call" in err["message"]

        # hang up from the dashboard
        assert client.post(f"/api/calls/{web[0]['id']}/hangup").status_code == 200
        msgs = []
        with pytest.raises(WebSocketDisconnect):
            while True:
                msgs.append(json.loads(ws.receive_text()))
        assert any(m.get("type") == "ended" for m in msgs)
    assert client.get("/api/status").json()["busy_cameras"] == []
    hist = client.get("/api/calls").json()["history"]
    assert hist[0]["direction"] == "web" and hist[0]["result"] == "hung up from web UI"


def test_web_call_requires_login(client):
    cam = _camera(client)
    client.post("/api/logout")
    client.cookies.clear()
    with client.websocket_connect(f"/api/cameras/{cam['id']}/talk") as ws:
        assert json.loads(ws.receive_text())["message"] == "not authenticated"
    with client.websocket_connect(f"/api/cameras/{cam['id']}/video") as ws:
        assert json.loads(ws.receive_text())["message"] == "not authenticated"


def test_video_proxy_reports_go2rtc_down(client):
    cam = _camera(client)
    with client.websocket_connect(f"/api/cameras/{cam['id']}/video") as ws:
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "error" and "go2rtc unavailable" in msg["message"]
