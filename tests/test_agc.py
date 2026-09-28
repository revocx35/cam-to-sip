import math
import os
import random
import shutil
import time

import pytest

from cam2sip.engine import CameraLink
from cam2sip.media import g711
from cam2sip.media.agc import Agc
from cam2sip.models import Camera

RATE = 8000
CHUNK = 1024                   # Tapo delivers 128 ms at a time


def noise(db: float, n: int, rng: random.Random) -> list[int]:
    sigma = 32768 * 10 ** (db / 20)
    return [max(-32767, min(32767, round(rng.gauss(0, sigma)))) for _ in range(n)]


def talk(db: float, seconds: float, noise_db: float, rng: random.Random, on=0.25, off=0.15):
    """Syllable-like 400 Hz bursts at `db` RMS over room noise. Returns (G.711 bytes, burst spans)."""
    amp = 32768 * 10 ** (db / 20) * math.sqrt(2)
    n = int(seconds * RATE)
    pcm = noise(noise_db, n, rng)
    spans, period = [], int((on + off) * RATE)
    for start in range(0, n, period):
        end = min(n, start + int(on * RATE))
        for i in range(start, end):
            pcm[i] = max(-32767, min(32767, pcm[i] + round(amp * math.sin(2 * math.pi * 400 * i / RATE))))
        spans.append((start, end))
    return g711.encode_pcm(pcm, "PCMA"), spans


def run(agc: Agc, data: bytes, hold=False) -> bytes:
    return b"".join(agc.process(data[i:i + CHUNK], "PCMA", hold) for i in range(0, len(data), CHUNK))


def burst_level(out: bytes, spans, after: float) -> float:
    lv = [g711.level_dbfs(out[s:e], "PCMA") for s, e in spans if s >= after * RATE]
    return sum(lv) / len(lv)


def test_level_and_peak_helpers():
    rng = random.Random(1)
    for codec in ("PCMA", "PCMU"):
        payload = bytes(rng.randrange(256) for _ in range(160))
        dec = [g711.DECODE[codec][b] for b in payload]
        mean = sum(v * v for v in dec) / len(dec)
        assert g711.level_dbfs(payload, codec) == pytest.approx(10 * math.log10(mean / 32768 ** 2))
        peak = max(abs(v) for v in dec)
        assert g711.peak_dbfs(payload, codec) == pytest.approx(20 * math.log10(peak / 32768))
        assert g711.peak_dbfs(g711.silence(codec), codec) < -60
        assert g711.peak_dbfs(g711.tone(codec, 1000, 0.1, -6), codec) == pytest.approx(-6, abs=0.3)
        signs = [v < 0 for v in dec]
        assert g711.crossings(payload, codec) == sum(a != b for a, b in zip(signs, signs[1:]))
        assert g711.crossings(g711.tone(codec, 400, 1.0, -20), codec) == pytest.approx(800, abs=4)


def test_distant_speech_is_lifted_to_target():
    rng = random.Random(2)
    data, spans = talk(-46, 6, -62, rng)
    agc = Agc(target_db=-20, max_gain_db=30)
    out = run(agc, data)
    assert len(out) == len(data)
    assert burst_level(out, spans, after=3) == pytest.approx(-20, abs=2.5)
    assert 22 < agc.gain_db <= 30


def test_gain_is_capped_by_max_gain():
    rng = random.Random(3)
    data, spans = talk(-50, 5, -65, rng)
    agc = Agc(target_db=-20, max_gain_db=12)
    out = run(agc, data)
    assert agc.gain_db <= 12
    assert burst_level(out, spans, after=3) == pytest.approx(-38, abs=2)


def test_room_noise_is_not_pumped_up():
    rng = random.Random(4)
    data = g711.encode_pcm(noise(-60, 8 * RATE, rng), "PCMA")
    agc = Agc()
    out = run(agc, data)
    assert agc.gain_db < 1.0
    assert g711.level_dbfs(out[-RATE:], "PCMA") == pytest.approx(-60, abs=1.5)


def test_clicks_do_not_raise_the_gain():
    rng = random.Random(9)
    pcm = noise(-62, 10 * RATE, rng)
    for t in range(1, 10):                         # a knock / clink every second: 100 ms of broadband noise
        burst = noise(-38, RATE // 10, rng)
        pcm[t * RATE:t * RATE + len(burst)] = [a + b for a, b in zip(pcm[t * RATE:], burst)]
    agc = Agc()
    run(agc, g711.encode_pcm([max(-32767, min(32767, v)) for v in pcm], "PCMA"))
    assert agc.gain_db < 1.0


def rumble(db: float, n: int, rng: random.Random, a=0.93) -> list[float]:
    out, y = [], 0.0
    for _ in range(n):
        y = a * y + rng.gauss(0, 1)
        out.append(y)
    scale = 32768 * 10 ** (db / 20) / math.sqrt(sum(v * v for v in out) / n)
    return [v * scale for v in out]


@pytest.mark.parametrize("step_db", [10, 20])
def test_a_fan_switching_on_does_not_raise_the_gain(step_db):
    rng = random.Random(10)
    pcm = noise(-62, 20 * RATE, rng)
    fan = rumble(-62 + step_db, 18 * RATE, rng)    # steady low rumble from 2 s on
    pcm[2 * RATE:] = [a + b for a, b in zip(pcm[2 * RATE:], fan)]
    agc = Agc()
    run(agc, g711.encode_pcm([max(-32767, min(32767, round(v))) for v in pcm], "PCMA"))
    assert agc.gain_db < 1.0


@pytest.mark.skipif(not shutil.which("espeak-ng"), reason="needs espeak-ng")
async def test_real_speech_far_from_the_camera_is_lifted(tmp_path):
    from cam2sip.media.tts import Tts
    tts = Tts(data_dir=str(tmp_path))
    sp = await tts.pcm("Hello, is anybody there? I am at the gate with a parcel for you.", "en-us", 150)
    await tts.close()
    rng = random.Random(11)
    rms = math.sqrt(sum(x * x for x in sp) / len(sp))
    g = 32768 * 10 ** (-45 / 20) / rms              # someone far away: -45 dBFS
    room = noise(-62, 2 * RATE + len(sp), rng)
    pcm = room[:2 * RATE] + [round(x * g) + r for x, r in zip(sp, room[2 * RATE:])]
    data = g711.encode_pcm([max(-32767, min(32767, v)) for v in pcm], "PCMA")
    agc = Agc(target_db=-20, max_gain_db=24)
    out = run(agc, data)
    assert agc.gain_db > 15
    tail = slice(len(data) // 2, len(data))         # second half of the sentence
    assert g711.level_dbfs(out[tail], "PCMA") > g711.level_dbfs(data[tail], "PCMA") + 15
    assert g711.level_dbfs(out[:2 * RATE], "PCMA") == pytest.approx(-62, abs=1.5)   # silence before: untouched


def test_gain_holds_in_pauses_after_speech():
    rng = random.Random(5)
    data, _ = talk(-46, 4, -62, rng)
    agc = Agc(target_db=-20, max_gain_db=24)
    run(agc, data)
    lifted = agc.gain_db
    assert lifted > 18
    run(agc, g711.encode_pcm(noise(-62, 5 * RATE, rng), "PCMA"))   # 5 s of silence
    assert agc.gain_db == pytest.approx(lifted, abs=0.5)


def test_loud_speech_is_never_attenuated_and_never_clips():
    rng = random.Random(6)
    agc = Agc(target_db=-20, max_gain_db=24)
    loud, _ = talk(-12, 2, -60, rng)
    assert run(agc, loud) == loud                  # above the target: left alone (0 dB)
    quiet, _ = talk(-46, 4, -62, rng)
    run(agc, quiet)
    assert agc.gain_db > 18
    out = run(agc, loud)                           # someone walks up to the camera and shouts
    peaks = [g711.peak_dbfs(out[i:i + 160], "PCMA") for i in range(0, len(out), 160)]
    assert max(peaks) <= -0.5
    assert agc.gain_db < 3


def test_hold_freezes_adaptation():
    rng = random.Random(7)
    data, _ = talk(-46, 4, -62, rng)
    agc = Agc()
    assert run(agc, data, hold=True) == data
    assert agc.gain_db == 0.0 and agc.floor_db is None


def test_camera_link_applies_agc_to_mic_audio():
    got = []
    plain = CameraLink(None, Camera(name="c", host="h"), lambda codec, p: got.append(p))
    assert plain.agc is None and plain.info()["mic"]["agc_gain_db"] is None
    chunk = os.urandom(CHUNK)
    plain._mic_audio("PCMA", chunk)
    assert got == [chunk]                          # off by default: bytes untouched

    rng = random.Random(8)
    data, _ = talk(-46, 4, -62, rng)
    cam = Camera(name="c", host="h", mic_agc=True, mic_agc_target_db=-20, mic_agc_max_gain_db=24)
    out = []
    link = CameraLink(None, cam, lambda codec, p: out.append(p))
    link._gate_open_until = time.monotonic() + 60  # camera speaker playing: AGC holds
    link._mic_audio("PCMA", data[:4 * CHUNK])
    assert link.agc.gain_db == 0.0
    link._gate_open_until = 0.0
    for i in range(0, len(data), CHUNK):
        link._mic_audio("PCMA", data[i:i + CHUNK])
    assert link.agc.gain_db > 18 and link.info()["mic"]["agc_gain_db"] > 0
    assert g711.level_dbfs(out[-1], "PCMA") > g711.level_dbfs(data[-CHUNK:], "PCMA") + 10


def test_camera_agc_settings_are_validated():
    with pytest.raises(ValueError):
        Camera(name="c", mic_agc_max_gain_db=50)
    with pytest.raises(ValueError):
        Camera(name="c", mic_agc_target_db=0)
    assert Camera(name="c").mic_agc is False


def test_camera_agc_api_roundtrip(tmp_path):
    from fastapi.testclient import TestClient

    from cam2sip.settings import Settings
    from cam2sip.web.app import create_app

    from .conftest import free_port
    s = Settings(data_dir=str(tmp_path), sip_port=free_port(), talk_port=free_port(), https_port=0,
                 go2rtc_api="http://127.0.0.1:9", rtp_port_min=31340, rtp_port_max=31359)
    with TestClient(create_app(s)) as c:
        c.post("/api/setup", json={"password": "secret1234"})
        cam = c.post("/api/cameras", json={"name": "Yard", "host": "10.0.0.7"}).json()
        assert cam["mic_agc"] is False and cam["mic_agc_target_db"] == -20 and cam["mic_agc_max_gain_db"] == 24
        cam = c.put(f"/api/cameras/{cam['id']}", json={"mic_agc": True, "mic_agc_max_gain_db": 18}).json()
        assert cam["mic_agc"] is True and cam["mic_agc_max_gain_db"] == 18
        bad = c.put(f"/api/cameras/{cam['id']}", json={"mic_agc_max_gain_db": 60})
        assert bad.status_code == 422 and "mic_agc_max_gain_db" in bad.json()["detail"]
