import json
import socket
import ssl
import urllib.request

import pytest
from fastapi.testclient import TestClient

from cam2sip.settings import Settings
from cam2sip.web.app import create_app

from .conftest import free_port


@pytest.fixture()
def client(tmp_path):
    s = Settings(data_dir=str(tmp_path), sip_port=free_port(), talk_port=free_port(),
                 https_port=free_port(socket.SOCK_STREAM),
                 go2rtc_api="http://127.0.0.1:9", rtp_port_min=31000, rtp_port_max=31019)
    app = create_app(s)
    with TestClient(app) as c:
        c.data_dir = tmp_path
        yield c


def test_setup_login_and_crud(client):
    assert client.get("/api/session").json()["setup_required"] is True
    assert client.get("/api/cameras").status_code == 401
    assert client.post("/api/setup", json={"password": "secret1234"}).status_code == 200
    assert client.post("/api/setup", json={"password": "again12345"}).status_code == 409

    cam = client.post("/api/cameras", json={"name": "Door", "kind": "tapo", "host": "10.0.0.5",
                                            "username": "u", "password": "p@ss", "cloud_password": "cloud"}).json()
    assert cam["password"] == "" and cam["password_set"] and cam["cloud_password_set"]
    # empty secret on update keeps the stored one
    client.put(f"/api/cameras/{cam['id']}", json={"name": "Front door", "password": ""})
    stored = json.loads((client.data_dir / "config.json").read_text())["cameras"][0]
    assert stored["name"] == "Front door" and stored["password"] == "p@ss"

    phone = client.post("/api/phones", json={"name": "Intercom", "server": "127.0.0.1", "port": 9,
                                             "username": "1008", "password": "x"}).json()
    assert phone["contact_user"].startswith("c2s-")
    assert client.post("/api/phones", json={"name": "", "server": "x", "username": "1"}).status_code == 422

    bridge = client.post("/api/bridges", json={"camera_id": cam["id"], "phone_id": phone["id"]}).json()
    assert bridge["speaker_gate_db"] == -50.0
    dup = client.post("/api/bridges", json={"camera_id": cam["id"], "phone_id": phone["id"]})
    assert dup.status_code == 409
    assert client.delete(f"/api/cameras/{cam['id']}").status_code == 409

    st = client.get("/api/status").json()
    assert st["counts"] == {"cameras": 1, "phones": 1, "bridges": 1}
    assert st["go2rtc"]["online"] is False

    # bearer token auth for automations
    token = client.get("/api/settings").json()["api_token"]
    client.post("/api/logout")
    client.cookies.clear()
    assert client.get("/api/bridges").status_code == 401
    ok = client.get("/api/bridges", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200 and len(ok.json()) == 1
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "secret1234"}).status_code == 200
    assert client.delete(f"/api/bridges/{bridge['id']}").status_code == 200
    assert client.delete(f"/api/cameras/{cam['id']}").status_code == 200


def test_https_listener_with_self_signed_cert(client):
    port = client.get("/api/session").json()["https_port"]
    assert (client.data_dir / "tls" / "cert.pem").exists()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    body = json.load(urllib.request.urlopen(f"https://127.0.0.1:{port}/api/health", context=ctx, timeout=5))
    assert body["ok"] is True


def test_camera_sources():
    from cam2sip.models import Camera
    tapo = Camera(name="t", kind="tapo", host="h", username="u@x", password="p:w", cloud_password="c/p")
    assert tapo.mic_source() == "rtsp://u%40x:p%3Aw@h:554/stream2"
    assert tapo.talk_source() == "tapo://c%2Fp@h?subtype=1"
    assert Camera(name="t", kind="tapo", host="h").talk_source() == ""
    onvif = Camera(name="o", kind="onvif", host="h", stream_path="/onvif/profile1?x=1")
    assert onvif.talk_source() == onvif.mic_source() == "rtsp://h:554/onvif/profile1?x=1"


def test_ivr_bridge_api(client):
    client.post("/api/setup", json={"password": "secret1234"})
    a = client.post("/api/cameras", json={"name": "Front", "kind": "tapo", "host": "10.0.0.5"}).json()
    b = client.post("/api/cameras", json={"name": "Garage", "kind": "tapo", "host": "10.0.0.6"}).json()
    ph = client.post("/api/phones", json={"name": "P", "server": "127.0.0.1", "port": 9, "username": "1"}).json()
    bad = client.post("/api/bridges", json={"phone_id": ph["id"], "mode": "ivr", "ivr_options": [
        {"digit": "1", "camera_id": a["id"]}, {"digit": "1", "camera_id": b["id"]}]})
    assert bad.status_code == 422 and "only be used once" in bad.json()["detail"]
    missing = client.post("/api/bridges", json={"phone_id": ph["id"], "mode": "ivr", "ivr_options": [
        {"digit": "1", "camera_id": "nope"}]})
    assert missing.status_code == 422
    ok = client.post("/api/bridges", json={"phone_id": ph["id"], "mode": "ivr", "ivr_voice": "en-us", "ivr_options": [
        {"digit": "1", "camera_id": a["id"]}, {"digit": "2", "camera_id": b["id"], "label": "the garage"}]})
    assert ok.status_code == 200 and ok.json()["mode"] == "ivr"
    assert client.delete(f"/api/cameras/{b['id']}").status_code == 409     # used by the menu
    wav = client.post("/api/ivr/preview", json=ok.json())
    assert wav.status_code == 200 and wav.content[:4] == b"RIFF"
    voices = client.get("/api/ivr/voices").json()
    assert "voices" in voices
    r = client.post(f"/api/bridges/{ok.json()['id']}/call", json={"target": "100", "camera_id": "other"})
    assert r.status_code == 409   # phone not registered (checked before camera membership is irrelevant)
