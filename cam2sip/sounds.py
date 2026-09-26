"""User-uploaded sounds for prompts (IVR menu parts, camera call notices).

Sounds are stored as 8 kHz mono 16-bit WAV in <data>/sounds/<id>.wav with
metadata in config.json. The web UI decodes any browser-supported format and
uploads WAV; the API accepts PCM WAV of any rate / channels / bit depth.
"""

from __future__ import annotations

import array
import io
import logging
import time
import wave
from pathlib import Path

from .media.tts import _normalize, _resample_to_8k
from .models import Sound, new_id
from .store import Store

log = logging.getLogger("cam2sip.sounds")

MAX_SECONDS = 120
MAX_UPLOAD_BYTES = 30 * 1024 * 1024


def wav_to_8k(data: bytes) -> array.array:
    """Decode a PCM WAV (any rate, 8/16/24/32-bit, mono/stereo) to 8 kHz mono int16."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a PCM WAV file ({e})") from e
    if not frames or rate < 4000:
        raise ValueError("empty or unsupported WAV")
    if rate and len(frames) / (width * channels * rate) > MAX_SECONDS:
        raise ValueError(f"sound is longer than {MAX_SECONDS} s")
    if width == 1:
        mono = array.array("h", ((b - 128) << 8 for b in frames))
    elif width == 2:
        mono = array.array("h")
        mono.frombytes(frames[: len(frames) - len(frames) % 2])
    elif width in (3, 4):
        mono = array.array("h", (int.from_bytes(frames[i + width - 2:i + width], "little", signed=True)
                                 for i in range(0, len(frames) - width + 1, width)))
    else:
        raise ValueError(f"unsupported sample width {width * 8} bit")
    if channels > 1:
        mono = array.array("h", (sum(mono[i:i + channels]) // channels
                                 for i in range(0, len(mono) - channels + 1, channels)))
    return _normalize(_resample_to_8k(mono, rate))


def to_wav(samples: array.array) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


class SoundLibrary:
    def __init__(self, store: Store):
        self.store = store
        self.dir = Path(store.dir) / "sounds"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, array.array] = {}

    def path(self, sid: str) -> Path:
        return self.dir / f"{sid}.wav"

    def get(self, sid: str) -> Sound | None:
        return next((s for s in self.store.config.sounds if s.id == sid), None)

    def add(self, name: str, data: bytes) -> Sound:
        samples = wav_to_8k(data)
        sound = Sound(id=new_id(), name=(name.strip() or "sound")[:80],
                      duration=round(len(samples) / 8000, 2), created=time.time())
        self.path(sound.id).write_bytes(to_wav(samples))
        self._cache[sound.id] = samples
        self.store.config.sounds.append(sound)
        self.store.save()
        log.info("sound '%s' uploaded (%.1f s)", sound.name, sound.duration)
        return sound

    def load(self, sid: str) -> array.array | None:
        if not sid:
            return None
        hit = self._cache.get(sid)
        if hit is not None:
            return hit
        p = self.path(sid)
        if not self.get(sid) or not p.exists():
            return None
        with wave.open(str(p)) as w:
            samples = array.array("h")
            samples.frombytes(w.readframes(w.getnframes()))
        self._cache[sid] = samples
        return samples

    def delete(self, sid: str) -> None:
        self.store.config.sounds = [s for s in self.store.config.sounds if s.id != sid]
        self.store.save()
        self._cache.pop(sid, None)
        self.path(sid).unlink(missing_ok=True)
