'use strict';
/* AudioWorklet: browser microphone (usually 48 kHz) -> 8 kHz mono, 20 ms frames.
 * A biquad low-pass (3.4 kHz) avoids aliasing, then linear interpolation
 * resamples by the (possibly fractional) ratio. Frames go to the main thread. */

class Mic8k extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 8000;
    this.next = 0;        // position of the next output sample, relative to `prev`
    this.prev = 0;
    this.frame = new Float32Array(160);
    this.n = 0;
    const w0 = 2 * Math.PI * 3400 / sampleRate;
    const alpha = Math.sin(w0) / (2 * Math.SQRT1_2);
    const cos = Math.cos(w0);
    const a0 = 1 + alpha;
    this.b0 = (1 - cos) / 2 / a0;
    this.b1 = (1 - cos) / a0;
    this.b2 = this.b0;
    this.a1 = -2 * cos / a0;
    this.a2 = (1 - alpha) / a0;
    this.x1 = this.x2 = this.y1 = this.y2 = 0;
  }

  process(inputs, outputs) {
    const input = inputs[0] && inputs[0][0];
    if (outputs[0] && outputs[0][0]) outputs[0][0].fill(0);
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      const x = input[i];
      const y = this.b0 * x + this.b1 * this.x1 + this.b2 * this.x2 - this.a1 * this.y1 - this.a2 * this.y2;
      this.x2 = this.x1; this.x1 = x; this.y2 = this.y1; this.y1 = y;
      while (this.next <= 1) {
        this.frame[this.n++] = this.prev + (y - this.prev) * this.next;
        this.next += this.ratio;
        if (this.n === 160) {
          this.port.postMessage(this.frame.slice());
          this.n = 0;
        }
      }
      this.next -= 1;
      this.prev = y;
    }
    return true;
  }
}

registerProcessor('mic-8k', Mic8k);
