import array
import asyncio
import io
import math
import wave

import pytest
from fastapi.testclient import TestClient

from cam2sip.engine import CameraLink, Engine
from cam2sip.media.rtsp import RtspAudioClient
from cam2sip.models import Bridge, Camera
from cam2sip.settings import Settings
from cam2sip.store import Store
from cam2sip.web.app import create_app

from .conftest import free_port


def make_wav(seconds=0.5, rate=44100, channels=2, freq=440) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        a = array.array("h")
        for i in range(int(seconds * rate)):
            a.extend([int(8000 * math.sin(2 * math.pi * freq * i / rate))] * channels)
        w.writeframes(a.tobytes())
    return buf.getvalue()


def settings(tmp_path, **kw):
    return Settings(data_dir=str(tmp_path), sip_port=free_port(), talk_port=free_port(), https_port=0,
                    go2rtc_api="http://127.0.0.1:9", rtp_port_min=31300, rtp_port_max=31319, **kw)


def test_sounds_api(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as c:
        c.post("/api/setup", json={"password": "secret1"})
        r = c.post("/api/sounds?name=Door%20chime", content=make_wav(1.0), headers={"Content-Type": "audio/wav"})
        assert r.status_code == 200
        snd = r.json()
        assert snd["name"] == "Door chime" and abs(snd["duration"] - 1.0) < 0.02
        assert c.post("/api/sounds?name=x", content=b"junk").status_code == 400
        wav = c.get(f"/api/sounds/{snd['id']}.wav")
        assert wav.status_code == 200 and wav.content[:4] == b"RIFF"
        assert c.put(f"/api/sounds/{snd['id']}", json={"name": "Chime"}).json()["name"] == "Chime"

        cam = c.post("/api/cameras", json={"name": "Door", "host": "10.0.0.5", "notify_enabled": True,
                                           "notify_sound": snd["id"]}).json()
        assert cam["notify_sound"] == snd["id"]
        assert c.post("/api/cameras", json={"name": "X", "host": "h", "notify_sound": "nope"}).status_code == 422
        ph = c.post("/api/phones", json={"name": "P", "server": "127.0.0.1", "port": 9, "username": "1"}).json()
        br = c.post("/api/bridges", json={"phone_id": ph["id"], "mode": "ivr", "ivr_greeting_sound": snd["id"],
                                          "ivr_options": [{"digit": "1", "camera_id": cam["id"], "sound": snd["id"]}]})
        assert br.status_code == 200
        used = c.get("/api/sounds").json()[0]["used_by"]
        assert "camera 'Door'" in used and any("bridge" in u for u in used)
        assert c.delete(f"/api/sounds/{snd['id']}").status_code == 409

        # menu preview = greeting sound + gap + option sound
        prev = c.post("/api/ivr/preview", json=br.json())
        with wave.open(io.BytesIO(prev.content)) as w:
            assert abs(w.getnframes() / 8000 - (1.0 + 0.3 + 1.0)) < 0.05
        # single prompt preview (used by the camera notice form)
        one = c.post("/api/ivr/preview", json={"sound": snd["id"]})
        with wave.open(io.BytesIO(one.content)) as w:
            assert abs(w.getnframes() / 8000 - 1.0) < 0.02

        c.delete(f"/api/bridges/{br.json()['id']}")
        c.put(f"/api/cameras/{cam['id']}", json={"notify_sound": ""})
        assert c.delete(f"/api/sounds/{snd['id']}").status_code == 200
        assert c.get("/api/sounds").json() == []


@pytest.mark.asyncio
async def test_camera_notice_plays_before_mic_opens(tmp_path):
    store = Store(tmp_path)
    engine = Engine(settings(tmp_path), store)
    await engine.talk_server.start()
    at_camera = bytearray()

    async def fake_play(dst, src):   # stand-in for go2rtc pulling our talk stream into the camera
        client = RtspAudioClient(src, lambda codec, payload: at_camera.extend(payload))
        asyncio.create_task(client.run(reconnect=False))

    async def noop(*_a, **_k):
        return None
    engine.go2rtc.play = fake_play
    engine.go2rtc.stop_play = noop
    snd = engine.sounds.add("notice", make_wav(0.6, 16000, 1))
    cam = Camera(name="Door", host="10.0.0.5", cloud_password="x", notify_enabled=True, notify_sound=snd.id)
    far_end = bytearray()
    link = CameraLink(engine, cam, lambda codec, payload: far_end.extend(payload))
    try:
        link.start()
        assert link.mic_open is False and link.notice_state == "pending"
        link._mic_audio("PCMA", b"\x01" * 160)             # camera mic while the notice is pending
        assert not far_end                                   # ... is not shared
        link.start_notice()
        for _ in range(60):
            if link.notice_state in ("played", "skipped"):
                break
            if link.notice_state == "playing":
                link.speak(b"\x55" * 160, "PCMA")            # caller audio during the notice is held back
            await asyncio.sleep(0.05)
        assert link.notice_state == "played" and link.mic_open
        notice_bytes = 0.6 * 8000
        assert abs(len(far_end) - notice_bytes) <= 320       # the caller heard the notice
        assert len(at_camera) >= notice_bytes - 320          # the camera speaker got it
        assert b"\x55" * 160 not in bytes(at_camera)         # no caller audio mixed in
        link._mic_audio("PCMA", b"\x01" * 160)
        assert far_end.endswith(b"\x01" * 160)              # mic is open afterwards
    finally:
        await link.stop()
        await engine.talk_server.stop()


@pytest.mark.asyncio
async def test_menu_pcm_mixes_tts_and_sounds(tmp_path):
    store = Store(tmp_path)
    engine = Engine(settings(tmp_path), store)
    snd = engine.sounds.add("greet", make_wav(0.5))
    cam = Camera(name="Garage", host="h")
    store.config.cameras.append(cam)
    b = Bridge(phone_id="p", mode="ivr", ivr_greeting_sound=snd.id, ivr_voice="en-us",
               ivr_options=[{"digit": "1", "camera_id": cam.id}])
    pcm = await engine.menu_pcm(b)
    tts_only = await engine.tts.pcm("Press 1 for Garage.", b.ivr_voice, b.ivr_speed)
    assert len(pcm) == 4000 + 2400 + len(tts_only)
