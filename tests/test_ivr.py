import asyncio
import math

import pytest

from cam2sip.engine import Engine
from cam2sip.media import g711, tts
from cam2sip.media.dtmf import HIGH, KEYS, LOW, DtmfDetector
from cam2sip.media.rtp import PortAllocator
from cam2sip.models import Bridge, Camera, Phone
from cam2sip.settings import Settings
from cam2sip.sip.ua import Account, AccountConfig, UserAgent
from cam2sip.store import Store

from .conftest import free_port


def dtmf_tone(digit: str, ms: int = 80) -> list[int]:
    row = next(i for i, r in enumerate(KEYS) if digit in r)
    col = KEYS[row].index(digit)
    f1, f2 = LOW[row], HIGH[col]
    n = 8 * ms
    return [int(7000 * (math.sin(2 * math.pi * f1 * i / 8000) + math.sin(2 * math.pi * f2 * i / 8000))) for i in range(n)]


def through_g711(samples: list[int]) -> list[int]:
    return [g711.DECODE["PCMU"][g711.linear_to_ulaw(s)] for s in samples]


def test_inband_dtmf_detection():
    det = DtmfDetector()
    silence = [0] * 800
    got = []
    for d in "159*0#":
        for chunk in (through_g711(dtmf_tone(d)), silence):
            for i in range(0, len(chunk), 160):     # 20 ms frames like RTP
                r = det.feed(chunk[i:i + 160])
                if r:
                    got.append(r)
    assert "".join(got) == "159*0#"
    # a held key is reported once; speech-like single tones and noise are ignored
    det2 = DtmfDetector()
    held = through_g711(dtmf_tone("7", 400))
    assert [d for d in (det2.feed(held[i:i + 160]) for i in range(0, len(held), 160)) if d] == ["7"]
    tone = [int(9000 * math.sin(2 * math.pi * 1000 * i / 8000)) for i in range(4000)]
    import random
    noise = [random.randint(-8000, 8000) for _ in range(4000)]
    det3 = DtmfDetector()
    assert not any(det3.feed(x[i:i + 160]) for x in (tone, noise) for i in range(0, 4000, 160))


def test_menu_text():
    text = tts.menu_text("Hello.", "Press {digit} for {name}.", [("1", "Front door"), ("2", "Garage")])
    assert text == "Hello. Press 1 for Front door. Press 2 for Garage."
    assert tts.fill("{name} is busy", name="Garage") == "Garage is busy"


@pytest.mark.asyncio
async def test_tts_renders_8k_audio():
    engine = tts.Tts()
    pcm = await engine.pcm("Press 1 for front door.", "en-us", 150)
    if engine.available:
        assert 0.8 < len(pcm) / 8000 < 5 and max(abs(s) for s in pcm) > 8000
    else:
        assert len(pcm) == 2000                       # beep fallback
    assert await engine.pcm("Press 1 for front door.", "en-us", 150) is pcm   # cached
    wav = await engine.wav("Hi", "en-us", 150)
    assert wav[:4] == b"RIFF"


@pytest.mark.asyncio
async def test_ivr_call_switches_cameras(tmp_path):
    settings = Settings(data_dir=str(tmp_path), sip_port=free_port(), talk_port=free_port(), https_port=0,
                        go2rtc_api="http://127.0.0.1:9", advertise_ip="127.0.0.1",
                        rtp_port_min=31200, rtp_port_max=31219)
    store = Store(tmp_path)
    cam_a = Camera(name="Front door", host="10.0.0.1")
    cam_b = Camera(name="Garage", host="10.0.0.2")
    phone = Phone(name="Intercom", server="127.0.0.1", port=9, username="1008", password="x")
    bridge = Bridge(phone_id=phone.id, mode="ivr", ivr_greeting="Hi.",
                    ivr_options=[{"digit": "1", "camera_id": cam_a.id}, {"digit": "2", "camera_id": cam_b.id}])
    store.config.cameras += [cam_a, cam_b]
    store.config.phones.append(phone)
    store.config.bridges.append(bridge)
    engine = Engine(settings, store)
    await engine.start()
    caller_ua = UserAgent(free_port(), PortAllocator(31220, 31239), advertise_ip="127.0.0.1")
    await caller_ua.start()
    acc = Account(caller_ua, AccountConfig(id="c", server="127.0.0.1", port=settings.sip_port,
                                           username="1001", password="x", contact_user="caller"))
    caller_ua.accounts["c"] = acc
    try:
        call = await acc.dial(f"{phone.contact_user}@127.0.0.1:{settings.sip_port}")
        heard = bytearray()
        call.rtp.on_packet = lambda p: heard.extend(p.payload)
        await asyncio.wait_for(call.answered.wait(), 5)
        await asyncio.sleep(1.5)                             # menu is being spoken
        assert heard and g711.level_dbfs(bytes(heard[-4000:]), call.codec) > -40
        session = next(iter(engine.sessions.values()))
        assert session.phase == "menu" and not engine.busy

        async def press(d: str) -> None:
            await call.send_dtmf(d)
            await asyncio.sleep(0.3)

        await press("9")                                     # invalid choice -> stays in menu
        assert session.phase == "menu"
        await asyncio.sleep(1.5)                             # let the "invalid" prompt finish
        await press("2")
        assert session.phase == "connected" and session.camera.id == cam_b.id
        assert engine.busy == {cam_b.id: next(iter(engine.ua.calls.values()))}
        assert engine.status()["calls"][0]["camera"] == "Garage"
        await press("*")                                     # back to the menu
        assert session.phase == "menu" and not engine.busy
        await press("1")
        assert session.camera.id == cam_a.id and cam_a.id in engine.busy
        await call.hangup()
        await asyncio.sleep(0.5)
        assert not engine.busy and not engine.sessions
        assert store.history[-1]["camera"] == "Garage, Front door"
    finally:
        await caller_ua.stop()
        await engine.stop()
