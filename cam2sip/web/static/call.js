'use strict';
/* Browser <-> camera call.
 * Audio: G.711 A-law 8 kHz over a WebSocket (/api/cameras/<id>/talk), both ways.
 * Video: go2rtc fMP4 via Media Source Extensions (/api/cameras/<id>/video),
 *        falling back to JPEG snapshots when MSE isn't available. */

const ALAW_DECODE = (() => {
  const t = new Float32Array(256);
  for (let i = 0; i < 256; i++) {
    const a = i ^ 0x55;
    let v = (a & 0x0f) << 4;
    const seg = (a & 0x70) >> 4;
    if (seg === 0) v += 8;
    else if (seg === 1) v += 0x108;
    else { v += 0x108; v <<= seg - 1; }
    t[i] = (a & 0x80 ? v : -v) / 32768;
  }
  return t;
})();
const ALAW_SEG_END = [0x1f, 0x3f, 0x7f, 0xff, 0x1ff, 0x3ff, 0x7ff, 0xfff];

function alawEncode(sample) {           // float -1..1 -> A-law byte
  let pcm = Math.max(-32768, Math.min(32767, Math.round(sample * 32767))) >> 3;
  let mask;
  if (pcm >= 0) mask = 0xd5;
  else { mask = 0x55; pcm = -pcm - 1; }
  let seg = 0;
  while (seg < 8 && pcm > ALAW_SEG_END[seg]) seg++;
  if (seg >= 8) return 0x7f ^ mask;
  const a = (seg << 4) | ((seg < 2 ? pcm >> 1 : pcm >> seg) & 0x0f);
  return a ^ mask;
}

/* Same resampler as mic-worklet.js, for the ScriptProcessor fallback. */
class Resampler8k {
  constructor(rate) {
    this.ratio = rate / 8000; this.next = 0; this.prev = 0; this.frame = new Float32Array(160); this.n = 0;
    const w0 = 2 * Math.PI * 3400 / rate, alpha = Math.sin(w0) / (2 * Math.SQRT1_2), cos = Math.cos(w0), a0 = 1 + alpha;
    this.b0 = (1 - cos) / 2 / a0; this.b1 = (1 - cos) / a0; this.b2 = this.b0; this.a1 = -2 * cos / a0; this.a2 = (1 - alpha) / a0;
    this.x1 = this.x2 = this.y1 = this.y2 = 0;
  }
  push(input, onFrame) {
    for (let i = 0; i < input.length; i++) {
      const x = input[i];
      const y = this.b0 * x + this.b1 * this.x1 + this.b2 * this.x2 - this.a1 * this.y1 - this.a2 * this.y2;
      this.x2 = this.x1; this.x1 = x; this.y2 = this.y1; this.y1 = y;
      while (this.next <= 1) {
        this.frame[this.n++] = this.prev + (y - this.prev) * this.next;
        this.next += this.ratio;
        if (this.n === 160) { onFrame(this.frame.slice()); this.n = 0; }
      }
      this.next -= 1;
      this.prev = y;
    }
  }
}

const withTimeout = (promise, ms, what) => Promise.race([
  promise, new Promise((_, reject) => setTimeout(() => reject(new Error(`${what} timed out`)), ms))]);

const wsUrl = path => `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${path}`;
const VIDEO_CODECS = ['avc1.640029', 'avc1.64002A', 'avc1.640033', 'hvc1.1.6.L153.B0'];
const PLAYOUT_DELAY = 0.2;              // seconds of camera audio buffered in the browser

class CameraCall {
  constructor(cameraId, handlers = {}) {
    this.id = cameraId;
    this.h = handlers;                  // onState(state, detail), onStatus(msg), onVideo(kind)
    this.talking = false;
    this.openMic = false;
    this.micGain = 1;
    this.levels = { camera: 0, mic: 0 };
    this.micState = 'off';
    this.closed = false;
    this.timers = [];
  }

  async start(video, img) {
    const AC = window.AudioContext || window.webkitAudioContext;
    this.ctx = new AC({ latencyHint: 'interactive' });
    this.out = this.ctx.createGain();
    this.out.connect(this.ctx.destination);
    this.playTime = 0;

    this.ws = new WebSocket(wsUrl(`/api/cameras/${encodeURIComponent(this.id)}/talk`));
    this.ws.binaryType = 'arraybuffer';
    this.ws.onopen = () => { this._state('connected'); this._sendMode(); };
    this.ws.onmessage = e => {
      if (typeof e.data === 'string') this._onText(JSON.parse(e.data));
      else this._play(new Uint8Array(e.data));
    };
    this.ws.onclose = e => { if (!this.closed) this._state('ended', e.reason || this.lastError || 'connection closed'); this.stop(); };

    this._startVideo(video, img);

    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      this.micState = 'insecure';
    } else {
      try { await this._startMic(); this.micState = 'ready'; }
      catch (e) { this.micState = 'denied'; this.micError = e.message || String(e); }
    }
    return this.micState;
  }

  get suspended() { return this.ctx && this.ctx.state === 'suspended'; }
  resume() { return this.ctx && this.ctx.resume(); }

  _state(state, detail) { if (this.h.onState) this.h.onState(state, detail); }

  _onText(msg) {
    if (msg.type === 'status' && this.h.onStatus) this.h.onStatus(msg);
    else if (msg.type === 'error') { this.lastError = msg.message; this._state('error', msg.message); }
    else if (msg.type === 'ended') { this.lastError = msg.reason; }
  }

  /* ---- camera -> speakers ---- */
  _play(bytes) {
    if (this.closed) return;
    const ctx = this.ctx, n = bytes.length, sr = ctx.sampleRate;
    const pcm = new Float32Array(n);
    let sum = 0;
    for (let i = 0; i < n; i++) { const v = ALAW_DECODE[bytes[i]]; pcm[i] = v; sum += v * v; }
    this.levels.camera = Math.sqrt(sum / n);
    const outLen = Math.round(n * sr / 8000);
    const buf = ctx.createBuffer(1, outLen, sr);
    const ch = buf.getChannelData(0);
    const step = 8000 / sr;
    for (let j = 0; j < outLen; j++) {
      const pos = j * step, i0 = Math.floor(pos), f = pos - i0;
      const a = pcm[i0], b = i0 + 1 < n ? pcm[i0 + 1] : a;
      ch[j] = a + (b - a) * f;
    }
    const now = ctx.currentTime;
    if (this.playTime < now + 0.02) this.playTime = now + PLAYOUT_DELAY;        // (re)start buffering
    else if (this.playTime > now + PLAYOUT_DELAY + 0.6) return;                 // too far behind: drop
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.out);
    src.start(this.playTime);
    this.playTime += outLen / sr;
  }

  setVolume(v) { if (this.out) this.out.gain.value = v; }

  /* ---- microphone -> camera ---- */
  async _startMic() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    const src = this.ctx.createMediaStreamSource(this.stream);
    const sink = this.ctx.createGain();
    sink.gain.value = 0;                 // keep the capture node pulled without hearing yourself
    sink.connect(this.ctx.destination);
    if (this.ctx.audioWorklet) {
      try {
        await withTimeout(this.ctx.audioWorklet.addModule('/static/mic-worklet.js'), 4000, 'AudioWorklet');
        this.node = new AudioWorkletNode(this.ctx, 'mic-8k', { numberOfOutputs: 1, outputChannelCount: [1] });
        this.node.port.onmessage = e => this._micFrame(e.data);
        src.connect(this.node).connect(sink);
        return;
      } catch (e) {
        console.warn('AudioWorklet unavailable, falling back to ScriptProcessor:', e.message);
      }
    }
    const rs = new Resampler8k(this.ctx.sampleRate);
    this.node = this.ctx.createScriptProcessor(2048, 1, 1);
    this.node.onaudioprocess = e => rs.push(e.inputBuffer.getChannelData(0), f => this._micFrame(f));
    src.connect(this.node).connect(sink);
  }

  _micFrame(frame) {
    let sum = 0;
    const out = new Uint8Array(frame.length);
    for (let i = 0; i < frame.length; i++) {
      const s = frame[i] * this.micGain;
      sum += s * s;
      out[i] = alawEncode(s);
    }
    this.levels.mic = Math.sqrt(sum / frame.length);
    if ((this.talking || this.openMic) && this.ws && this.ws.readyState === 1) this.ws.send(out);
  }

  setTalking(on) { this.talking = on; this._sendMode(); }
  setOpenMic(on) { this.openMic = on; this._sendMode(); }
  _sendMode() {
    // push-to-talk sends everything while held; open mic uses the server's noise gate
    if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify({ type: 'gate', db: this.openMic && !this.talking ? -55 : null }));
  }

  /* ---- video ---- */
  _startVideo(video, img) {
    const MS = window.ManagedMediaSource || window.MediaSource;
    const codecs = MS ? VIDEO_CODECS.filter(c => MS.isTypeSupported(`video/mp4; codecs="${c}"`)) : [];
    if (!codecs.length) return this._snapshots(video, img);
    const ms = new MS();
    if (window.ManagedMediaSource) { video.disableRemotePlayback = true; video.srcObject = ms; }
    else video.src = URL.createObjectURL(ms);
    video.muted = true;
    video.playsInline = true;
    let gotData = false;
    ms.addEventListener('sourceopen', () => {
      const vws = this.vws = new WebSocket(wsUrl(`/api/cameras/${encodeURIComponent(this.id)}/video`));
      vws.binaryType = 'arraybuffer';
      let sb = null;
      const queue = [];
      const pump = () => {
        if (!sb || sb.updating) return;
        if (queue.length) {
          const parts = queue.splice(0);
          const size = parts.reduce((n, p) => n + p.byteLength, 0);
          const data = new Uint8Array(size);
          let off = 0;
          for (const p of parts) { data.set(new Uint8Array(p), off); off += p.byteLength; }
          try { sb.appendBuffer(data); } catch { /* quota / closed */ }
          return;
        }
        const b = sb.buffered;
        if (b.length) {
          const end = b.end(b.length - 1);
          if (end - video.currentTime > 1.2) video.currentTime = end - 0.2;     // stay live
          if (video.currentTime - b.start(0) > 10) { try { sb.remove(b.start(0), video.currentTime - 5); } catch { /* busy */ } }
        }
      };
      vws.onopen = () => vws.send(JSON.stringify({ type: 'mse', value: codecs.join() }));
      vws.onmessage = e => {
        if (typeof e.data === 'string') {
          const m = JSON.parse(e.data);
          if (m.type === 'mse') {
            sb = ms.addSourceBuffer(m.value);
            sb.mode = 'segments';
            sb.addEventListener('updateend', pump);
            if (this.h.onVideo) this.h.onVideo('live');
          } else if (m.type === 'error') { vws.close(); }
          return;
        }
        gotData = true;
        queue.push(e.data);
        pump();
      };
      vws.onclose = () => { if (!this.closed && !gotData) this._snapshots(video, img); };
    }, { once: true });
    video.play().catch(() => {});
  }

  _snapshots(video, img) {
    if (this.closed || this.snapTimer) return;
    video.classList.add('hidden');
    img.classList.remove('hidden');
    if (this.h.onVideo) this.h.onVideo('snapshot');
    const next = () => {
      if (this.closed) return;
      img.onload = img.onerror = () => { this.snapTimer = setTimeout(next, 1000); };
      img.src = `/api/cameras/${encodeURIComponent(this.id)}/snapshot.jpg?t=${Date.now()}`;
    };
    this.snapTimer = setTimeout(next, 0);
  }

  stop() {
    if (this.closed) return;
    this.closed = true;
    clearTimeout(this.snapTimer);
    try { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify({ type: 'hangup' })); } catch { /* closing */ }
    try { this.ws && this.ws.close(); } catch { /* closed */ }
    try { this.vws && this.vws.close(); } catch { /* closed */ }
    if (this.stream) this.stream.getTracks().forEach(t => t.stop());
    if (this.ctx) this.ctx.close().catch(() => {});
  }
}
