#!/usr/bin/env python3
"""Place a real test call into a cam2sip bridge and measure audio both ways.

Registers nothing: it sends an authenticated INVITE through the PBX (or
directly to cam2sip), plays a tone into the call (-> camera speaker) and
records what comes back (<- camera microphone).

Run inside the dev image with host networking, e.g.:

    docker run --rm --network host -v $PWD:/app -w /app cam2sip-dev \
      python tools/sip_test_call.py --server 192.168.1.10 --user 1001 \
        --password secret --target 1008 --wav /app/data/test_rx.wav

The caller account may even be the bridged extension itself (Asterisk then
routes the call to cam2sip's registration).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cam2sip.media import g711  # noqa: E402
from cam2sip.media.rtp import PortAllocator  # noqa: E402
from cam2sip.sip.ua import Account, AccountConfig, UserAgent  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    ua = UserAgent(args.local_port, PortAllocator(args.rtp_port, args.rtp_port + 20),
                   user_agent="cam2sip-test-call")
    await ua.start()
    acc = Account(ua, AccountConfig(id="test", server=args.server, port=args.port, username=args.user,
                                    password=args.password, contact_user="cam2sip-test-call"))
    ua.accounts["test"] = acc
    rx = bytearray()
    try:
        call = await acc.dial(args.target)

        def on_packet(pkt):
            if call.negotiated and pkt.pt == call.negotiated.remote_pt:
                rx.extend(pkt.payload)
        call.rtp.on_packet = on_packet
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(call.answered.wait(), args.ring_timeout)
        except TimeoutError:
            print(f"FAIL: not answered ({call.state}: {call.end_reason or 'timeout'})")
            await call.hangup()
            return 1
        print(f"answered after {time.monotonic() - t0:.1f}s, codec {call.codec}")
        codec, pt = call.codec, call.negotiated.remote_pt
        loop = asyncio.get_running_loop()
        await asyncio.sleep(args.listen_before)
        for digit in [d for d in (args.dtmf or "").split(",") if d]:
            mark = len(rx)
            await call.send_dtmf(digit)
            print(f"pressed {digit} (at {mark / 8000:.1f}s of received audio)")
            await asyncio.sleep(args.dtmf_interval)
        frames = g711.tone(codec, args.tone_hz, args.tone_seconds, -8) + g711.silence(codec, 8000)
        t = loop.time()
        for i in range(0, len(frames), 160):
            call.rtp.send(pt, frames[i:i + 160], 160)
            t += 0.02
            await asyncio.sleep(max(0.0, t - loop.time()))
        stats = call.info()["rtp"]
        await call.hangup()
        levels = [round(g711.level_dbfs(bytes(rx[i:i + 4000]), codec), 1) for i in range(0, len(rx) - 3999, 4000)]
        print(f"RTP: {stats['tx_packets']} sent, {stats['rx_packets']} received")
        print("received audio level per 0.5 s (dBFS):", levels)
        if args.wav:
            Path(args.wav).write_bytes(g711.to_wav(bytes(rx), codec))
            print("received audio saved to", args.wav)
        if not rx:
            print("FAIL: no audio received from the camera")
            return 1
        if max(levels or [-96]) < -90:
            print("WARN: received only digital silence (camera microphone not connected?)")
        print("OK")
        return 0
    finally:
        await ua.stop()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", required=True, help="PBX (or cam2sip) host")
    p.add_argument("--port", type=int, default=5060)
    p.add_argument("--user", required=True, help="caller extension / auth user")
    p.add_argument("--password", default="")
    p.add_argument("--target", required=True, help="number to call, e.g. the bridged extension")
    p.add_argument("--local-port", type=int, default=5070)
    p.add_argument("--rtp-port", type=int, default=17000)
    p.add_argument("--ring-timeout", type=float, default=20)
    p.add_argument("--listen-before", type=float, default=2.0, help="seconds of listening before the tone")
    p.add_argument("--tone-hz", type=float, default=700)
    p.add_argument("--tone-seconds", type=float, default=3.0)
    p.add_argument("--dtmf", help="comma separated keys to press after --listen-before, e.g. 2,*,1 (IVR test)")
    p.add_argument("--dtmf-interval", type=float, default=4.0, help="seconds between key presses")
    p.add_argument("--wav", help="save received audio to this WAV file")
    p.add_argument("-v", "--verbose", action="store_true", help="print SIP messages")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.verbose:
        logging.getLogger("cam2sip.sip.trace").setLevel(logging.DEBUG)
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
