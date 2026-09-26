#!/usr/bin/env python3
"""Headless-browser smoke test of a web call (video + push-to-talk).

Runs Firefox with a fake microphone against a running cam2sip and checks that
the live video plays (MSE), the microphone starts and push-to-talk reaches the
camera speaker. Run it in the Playwright image; it needs a PulseAudio sink,
because headless browsers have no audio device otherwise:

    docker run --rm --network host -v $PWD:/repo mcr.microsoft.com/playwright/python:v1.55.0-noble \
      bash -c "apt-get update -qq && apt-get install -y -qq pulseaudio >/dev/null && \
               pulseaudio -D --exit-idle-time=-1 && sleep 3 && \
               pactl load-module module-null-sink sink_name=null >/dev/null && \
               pip install -q playwright==1.55.0 && \
               python /repo/tools/browser_call_test.py --password <admin> --camera <camera-id>"

Quirks: Playwright's Chromium has no H.264 (video falls back to snapshots) and
its AudioWorklet never loads in a container (the ScriptProcessor fallback is
used), so Firefox + PulseAudio is the representative combination.
"""

import argparse
import asyncio
import sys

from playwright.async_api import async_playwright


async def run(args) -> int:
    async with async_playwright() as p:
        browser = await p.firefox.launch(firefox_user_prefs={
            "media.navigator.streams.fake": True, "media.navigator.permission.disabled": True,
            "media.autoplay.default": 0})
        page = await browser.new_page(viewport={"width": 1280, "height": 760})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(args.url)
        await page.fill("#auth-password", args.password)
        await page.click("#auth-submit")
        await page.wait_for_selector("#app:not(.hidden)")
        await page.goto(f"{args.url}/#/call/{args.camera}")
        await page.wait_for_selector("#ptt:not([disabled])", timeout=20000)
        await page.wait_for_timeout(4000)
        video = await page.evaluate("""(() => { const v = document.querySelector('#call-video');
            return {playing: v.readyState >= 2 && v.videoWidth > 0, size: `${v.videoWidth}x${v.videoHeight}`,
                    snapshot: !document.querySelector('#call-img').classList.contains('hidden')}; })()""")
        await page.keyboard.down("Space")
        await page.wait_for_timeout(2500)
        stats = (await page.inner_text("#call-stats")).replace("\n", " ")
        await page.keyboard.up("Space")
        if args.screenshot:
            await page.screenshot(path=args.screenshot)
        await page.click("#call-hangup")
        await browser.close()
    print("video:", video)
    print("status while talking:", stats)
    ok = (video["playing"] or video["snapshot"]) and "live" in stats and "talking" in stats and not errors
    print("page errors:", errors or "none")
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8090", help="cam2sip base URL (localhost counts as secure)")
    ap.add_argument("--password", required=True, help="admin password")
    ap.add_argument("--camera", required=True, help="camera id")
    ap.add_argument("--screenshot", help="save a screenshot of the call page")
    sys.exit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
