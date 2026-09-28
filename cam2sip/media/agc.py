"""Adaptive gain control (AGC) for camera microphones.

Someone standing far from a camera arrives at -45 dBFS or less, which is hard
to hear on a phone. `Agc` lifts speech towards a target level:

* Audio is processed in 20 ms blocks. Each block gets one gain, applied with a
  single ``bytes.translate`` (``g711.convert``), so there is no per-sample loop.
* A block sounds like voice when it is at least ``SPEECH_MARGIN_DB`` above a
  tracked noise floor and voiced: fewer than ``MAX_VOICE_CROSSINGS`` zero
  crossings per second (vowels ~1000-2600, room hiss ~2900, clicks 3700+).
* The gain only rises after a *syllable*: voice lasting ``SYLLABLE_MIN_S`` to
  ``SYLLABLE_MAX_S``, at least ``SYLLABLE_SNR_DB`` above the floor, followed within
  ``DIP_WAIT_S`` by a dip of at least ``DIP_DB`` below its level. Steady noise (a fan
  switching on, rain) doesn't look like that and clicks aren't voiced, so neither
  pumps the gain. In pauses it holds.
* It falls at once when a voice block comes out clearly too loud (someone right
  at the camera). It stays between 0 dB (never quieter than the camera) and
  ``max_gain_db``, and each block is limited so its peak stays below
  ``PEAK_CEILING_DB`` (no clipping).
* ``hold=True`` freezes adaptation. `CameraLink` sets it while the camera speaker
  plays, because echo-cancelling cameras (Tapo) duck their mic then and the AGC
  would otherwise chase the ducking and blast the caller once it ends.
"""

from __future__ import annotations

import math

from . import g711

BLOCK = 160                  # 20 ms at 8 kHz
SPEECH_MARGIN_DB = 6.0       # voice = this far above the noise floor ...
MIN_SPEECH_DB = -70.0        # ... and above this absolute level ...
MAX_VOICE_CROSSINGS = 3000   # ... with fewer zero crossings per second than this
SYLLABLE_MIN_S = 0.05        # 3 blocks
SYLLABLE_MAX_S = 1.5         # longer without a dip is a steady sound
DIP_DB = 6.0                 # a syllable ends with a drop this deep ...
DIP_WAIT_S = 0.25            # ... this soon after its last voice block
SYLLABLE_SNR_DB = 10.0       # and stands this far above the floor (noise pokes don't)
TOO_LOUD_DB = 6.0            # a voice block this far above the target lowers the gain at once
FLOOR_MIN_DB = -80.0         # digital silence must not drag the floor down forever
FLOOR_FALL_S = 0.2           # the floor follows quieter blocks quickly ...
FLOOR_RISE_DB_S = 2.0        # ... and louder ones slowly
LEVEL_ATTACK_S = 0.05        # speech level (per syllable): follows louder syllables at once,
LEVEL_RELEASE_S = 0.5        # quieter ones gradually
GAIN_UP_S = 0.25             # gain time constants: rise slowly, fall quickly
GAIN_DOWN_S = 0.1
PEAK_CEILING_DB = -1.0
GAIN_STEP_DB = 0.5           # quantised gain keeps the translate-table cache small


def _smooth(cur: float, target: float, dt: float, tc: float) -> float:
    return cur + (target - cur) * (1.0 - math.exp(-dt / tc))


class Agc:
    def __init__(self, target_db: float = -20.0, max_gain_db: float = 24.0):
        self.target_db = target_db
        self.max_gain_db = max(0.0, max_gain_db)
        self.gain_db = 0.0             # adapted gain (each block may get less: peak limit)
        self.floor_db: float | None = None
        self.speech_db: float | None = None
        self._run_s = 0.0              # the current sound: voice seconds ...
        self._run_energy = 0.0         # ... and their energy (power x seconds)
        self._quiet_s = 0.0            # quiet since its last voice block

    def process(self, payload: bytes, codec: str, hold: bool = False) -> bytes:
        """Return `payload` (G.711 in `codec`) with the adaptive gain applied."""
        out = []
        for i in range(0, len(payload), BLOCK):
            block = payload[i:i + BLOCK]
            if not hold:
                self._adapt(block, codec)
            gain = min(self.gain_db, PEAK_CEILING_DB - g711.peak_dbfs(block, codec))
            gain = max(0.0, math.floor(gain / GAIN_STEP_DB) * GAIN_STEP_DB)
            out.append(g711.convert(block, codec, codec, gain))
        return out[0] if len(out) == 1 else b"".join(out)

    def _adapt(self, block: bytes, codec: str) -> None:
        dt = len(block) / 8000
        level = g711.level_dbfs(block, codec)
        floor = self.floor_db
        if floor is None:
            floor = level
        elif level < floor:
            floor = _smooth(floor, level, dt, FLOOR_FALL_S)
        else:
            floor = min(level, floor + FLOOR_RISE_DB_S * dt)
        self.floor_db = floor = max(floor, FLOOR_MIN_DB)
        if level < max(floor + SPEECH_MARGIN_DB, MIN_SPEECH_DB):
            self._quiet(level, dt)
            return
        if g711.crossings(block, codec) * 8000 > MAX_VOICE_CROSSINGS * max(1, len(block) - 1):
            return                       # hiss, a click or an "s": neither voice nor a pause
        self._quiet_s = 0.0
        self._run_s += dt
        self._run_energy += dt * 10 ** (level / 10)
        if self._run_s > SYLLABLE_MAX_S:
            self._reset_run()            # steady sound, not speech
        if level + self.gain_db > self.target_db + TOO_LOUD_DB:
            self.gain_db = _smooth(self.gain_db, max(0.0, self.target_db - level), dt, GAIN_DOWN_S)

    def _quiet(self, level: float, dt: float) -> None:
        run_s = self._run_s
        if not run_s:
            return
        if run_s < SYLLABLE_MIN_S:
            self._reset_run()            # noise poking above the threshold
            return
        self._quiet_s += dt
        run_db = 10 * math.log10(self._run_energy / run_s)
        if level <= run_db - DIP_DB:
            self._reset_run()
            if run_db >= self.floor_db + SYLLABLE_SNR_DB:
                self._syllable(run_db, run_s)
        elif self._quiet_s > DIP_WAIT_S:
            self._reset_run()            # it faded without a pause: not a syllable

    def _syllable(self, level: float, seconds: float) -> None:
        speech = self.speech_db
        if speech is None:
            speech = level
        else:
            speech = _smooth(speech, level, seconds, LEVEL_ATTACK_S if level > speech else LEVEL_RELEASE_S)
        self.speech_db = speech
        want = min(self.max_gain_db, max(0.0, self.target_db - speech))
        self.gain_db = _smooth(self.gain_db, want, seconds, GAIN_DOWN_S if want < self.gain_db else GAIN_UP_S)

    def _reset_run(self) -> None:
        self._run_s = self._run_energy = self._quiet_s = 0.0
