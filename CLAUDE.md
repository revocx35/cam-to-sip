# CLAUDE.md

Guide for AI assistants (and humans) working on this repository.

## What this is

**cam2sip** bridges IP-camera two-way audio to SIP. It registers "virtual phones" (SIP accounts) on a PBX. When one is called, it auto-answers and connects the caller to a camera: camera mic → caller, caller → camera speaker. It can also dial out ("doorbell"), and the web UI can call a camera straight from the browser (WebSocket audio + MSE video, `webcall.py` / `static/call.js`). The deliverable is a Docker Compose stack of two containers, `cam2sip` (Python) and `go2rtc` (camera protocols), with a web UI on :8090.

Read [ARCHITECTURE.md](ARCHITECTURE.md) before changing call or media code. It has the component diagram, the call flows and the media pipeline.

## Commands

```bash
# run the stack (host networking)
docker compose up -d --build
docker logs -f cam2sip            # app logs
docker logs -f cam2sip-go2rtc     # go2rtc logs

# tests: the host may lack python venv/pip, so always use the dev container
docker build -t cam2sip-dev -f Dockerfile.dev .
docker run --rm -v $PWD:/app -w /app cam2sip-dev python -m pytest -q          # ~2 s

# live end-to-end call through a real PBX (needs --network host)
docker run --rm --network host -v $PWD:/app -w /app cam2sip-dev \
  python tools/sip_test_call.py --server <pbx> --user <ext> --password <secret> --target <bridged-ext>

# IVR: press keys during a live call (choose 2, back to menu, choose 1)
docker run --rm --network host -v $PWD:/app -w /app cam2sip-dev \
  python tools/sip_test_call.py --server <host> --port 5062 --user tester \
  --target <contact_user>@<host>:5062 --dtmf 2,*,1 --dtmf-interval 5

# browser call smoke test (Firefox + fake mic + PulseAudio; full docker command in the docstring)
python tools/browser_call_test.py --password <admin> --camera <camera-id>

# inspect go2rtc (bound to localhost only)
curl -s http://127.0.0.1:11984/api/streams | python3 -m json.tool
```

Local test-environment details (PBX, camera, credentials, where it's deployed) live in `CLAUDE.local.md`. That file is git-ignored; **never commit credentials**.

## Layout

- `cam2sip/sip/`: own SIP stack. `message.py` parses/builds, `auth.py` does digest, `sdp.py` handles SDP, `stack.py` has UDP + transactions, `ua.py` has accounts + calls.
- `cam2sip/media/`: `g711.py` (tables, level/peak/zero-crossing helpers), `rtp.py` (RTP socket, `Pacer` jitter buffer), `rtsp.py` (mic client + talk server for go2rtc), `agc.py` (adaptive mic gain, applied in `CameraLink._mic_audio`).
- `cam2sip/engine.py`: `Engine` (config → go2rtc streams + SIP accounts, call routing), `CameraLink` (camera side of any call: mic, speaker, gate, keep-alive) and `BridgeSession` (SIP call ↔ CameraLink, plus the IVR menu phases: menu ↔ connected).
- `cam2sip/media/tts.py` (espeak-ng prompts) and `media/dtmf.py` (in-band Goertzel detector) serve the IVR.
- `cam2sip/sounds.py`: uploaded sounds (`SoundLibrary`). `Engine.prompt_pcm()` picks the uploaded sound or TTS for any prompt; `CameraLink` plays the camera's privacy call notice (`start_notice()`, `mic_open`).
- `cam2sip/webcall.py`: `WebCall` (browser call over `WS /api/cameras/{id}/talk` ↔ CameraLink). The video WS proxy lives in `web/app.py`.
- `cam2sip/web/`: FastAPI app (`app.py`: proxy-header and auth middleware, routes), `auth.py` (scrypt, session cookie, `LoginThrottle`) plus a no-build vanilla-JS SPA in `static/`. ARCHITECTURE.md §7 has the request path and the reasoning.
- `cam2sip/models.py` / `store.py`: pydantic config persisted to `/data/config.json`.
- `tests/`: pytest (asyncio mode auto). `tests/test_sip_loopback.py` runs real SIP calls between two in-process UAs.
- `docs/`: user docs (FreePBX, cameras, API, troubleshooting).

## Conventions

- Everything runs on one asyncio loop (uvicorn's). Never block it: no sync sockets, no `time.sleep`, and no per-sample Python loops on the audio path. Use `g711.convert` (a `bytes.translate` table) for transcoding and gain.
- Media callbacks (`on_packet`, `on_audio`) run synchronously in the datagram/stream handler. Keep them cheap, and schedule heavier work with `asyncio.create_task`.
- Only G.711 (PCMA/PCMU) at 8 kHz on the SIP side. Other camera codecs get transcoded by go2rtc (`ffmpeg:` source).
- Secrets (`password`, `cloud_password`) are write-only in the API: return `""` + `<field>_set`, and keep the stored value when an update sends `""`. `web/app.py:merge()` does this.
- go2rtc stream names are `c2s_<camera id>` (mic) and `c2s_<camera id>_talk` (speaker). cam2sip owns every `c2s_*` stream and deletes unknown ones.
- UI: escape all user data with `esc()` in `app.js`. No frameworks and no build step.
- Web security (the UI may be exposed through a reverse proxy):
  - Every `/api/*` HTTP route gets the cross-site check and auth from `require_auth`. Add a route to `PUBLIC` only if it must work signed out.
  - WebSocket routes skip the HTTP middleware: call `ws_authed()` right after `accept()`. It checks who opened the socket (`Sec-Fetch-Site`, else `Origin` vs `Host`), then the cookie.
  - Anything that checks the admin password goes through `charge_attempt()` → `password_ok()` → `throttle.success()` (only after everything succeeded). Never compare passwords on the event loop.
  - Use `client_ip(request)` for the client address; don't read `X-Forwarded-For` yourself.
- When you change env vars, API endpoints or behaviour, update `README.md`, the `docs/` pages, `.env.example` and both compose files (`docker-compose.yml`, `deploy/docker-compose.yml`).
- Add or adjust tests for SIP/media changes. The loopback tests catch most dialog bugs quickly. Auth changes belong in `tests/test_login_security.py`.

## Hard-won facts (don't re-learn these)

- **Tapo sends no RTP when only its audio track is SETUP.** The mic URL must ask go2rtc for video too: `rtsp://127.0.0.1:18554/c2s_<id>?video&audio=pcma,pcmu`, while our client SETUPs only the audio track (`Camera.mic_with_video`).
- **Tapo speaker = `tapo://<cloud password>@ip`** (the TP-Link account password; the username is implicitly `admin`). The camera/RTSP account gives `401`. `tapo://admin:<cloudpw>@` also gives 401 on the C212 fw 1.5.1. The C212 has **no** ONVIF RTSP backchannel, although ONVIF reports an audio output.
- Tapo mic audio arrives in 1024-byte (128 ms) chunks. Tapo talk mode is `aec`, so the mic is ducked ~10 dB while the speaker plays. That's why the noise gate exists.
- go2rtc relabels PCMA as dynamic PT 96 on its RTSP server. Read codecs from the SDP rtpmap, not static PTs.
- go2rtc's RTSP server may assign interleaved channels other than 0. Parse them from the SETUP reply.
- go2rtc drops an RTSP *source* after 5 s without data. The talk path sends a silence keep-alive every 1 s while the gate is closed.
- go2rtc runs with an inline `-config` JSON (no file). `PUT /api/streams` then answers `400 config file disabled` but still creates the stream. `Go2rtc.put_stream` ignores that error.
- `POST /api/streams?dst=<talk stream>&src=<url>` makes go2rtc pull `src` into the backchannel of `dst`. Our `TalkServer` is that `src`. An empty `src` stops playback.
- Asterisk (FreePBX 17) picks PCMU for extensions even though we offer PCMA first. That's harmless: A↔μ conversion is a table lookup.
- Asterisk routes a call from extension X to X itself to X's registered contact, so `tools/sip_test_call.py` can use the bridged extension's own credentials as the caller.
- Browsers only give `getUserMedia` and AudioWorklets to secure contexts. That's why a second uvicorn listener serves TLS on :8443 (`web/tls.py` makes a self-signed certificate with the `openssl` CLI, which is present in `python:3.13-slim`). That listener runs with `lifespan="off"` and a no-op `capture_signals`; two servers capturing signals break shutdown.
- FastAPI's `@app.middleware("http")` does **not** run for WebSockets. WS endpoints must check the session cookie themselves (`ws_authed`).
- **Chromium sends no `Sec-Fetch-Site` on WebSocket handshakes** (Firefox and WebKit do); no browser sends `Sec-Fetch-*` to plain-HTTP origins. Measured with Playwright (Chromium/Firefox/WebKit) on 2026-09-27: v1.5.1 relied on `Sec-Fetch-Site` alone and a sibling-subdomain page could still open the camera sockets in Chromium. `Origin` is sent by every browser on WebSocket handshakes, so WS checks must use it (`foreign_origin()`).
- Headless browser testing: Playwright's Chromium has **no H.264** (MSE unsupported, so the page shows snapshots), and in a container its `audioWorklet.addModule()` never resolves (so `call.js` falls back to a ScriptProcessor after 4 s). Headless Firefox's AudioContext stays `suspended` without an audio device. Run PulseAudio with a null sink in the container, then Firefox exercises the full path (worklet + MSE).
- The keep-alive for go2rtc's talk source is timer-driven (`CameraLink._keepalive`), because browsers send nothing while push-to-talk is released.
- `espeak-ng --stdout` writes a WAV header with bogus sizes. `tts._parse_wav` reads the `data` chunk to EOF instead of trusting `wave`.
- IVR prompts must win over camera audio on the same 20 ms clock (`BridgeSession._to_phone`). Barge-in means `_on_dtmf` in the menu phase calls `_stop_prompt()`, and a digit queued during an announcement skips the next menu replay (`test_ivr.py` covers this).
- **Testing live without disturbing production:** run a local instance whose virtual phone points at a dead PBX (e.g. `127.0.0.1:9`), so it never registers the real extension (FreePBX `max_contacts=1` would steal the registration). Then INVITE it directly with `tools/sip_test_call.py --server <this host> --port 5062 --target <contact_user>@<host>:5062`. Inbound INVITEs are routed by the phone's `contact_user`.
- Call notice invariant: while `CameraLink.notice_state` is `pending` or `playing`, camera mic audio is dropped (`_mic_audio`) and far-end audio is not sent to the speaker (`speak`). Owners must call `start_notice()` only when the call is connected. `tests/test_sounds_notice.py` fakes go2rtc by pulling the talk stream with `RtspAudioClient`, which is a handy pattern for speaker-path tests.
- Sound uploads: the browser converts to 8 kHz WAV (`audioFileToWav` in app.js). The server only parses PCM WAV (stdlib `wave`), so there's no ffmpeg in the image. Upload bodies are raw WAV (no multipart and no python-multipart dependency).
- Natural TTS: `media/piper.py` runs `python -m piper.http_server` (GPL-3.0, separate process) on 127.0.0.1:18556; voice ids are `piper:<key>`, downloaded from rhasspy/piper-voices into /data/voices. Tests must never download: `tests/conftest.py` makes the catalog unavailable by default (fake it per test).
- Several bridges per phone: incoming calls are routed by caller in `Store.route_bridge()` (listed caller → default bridge → 403), and `Store.routing_conflict()` enforces unambiguous routing on save. Nothing may assume one bridge per phone.
- Client-transaction lingering uses `loop.call_later`, not sleeping tasks. Sleeping tasks made every test take 32 s and slowed shutdown.
- **Client IPs:** uvicorn's proxy-header handling is off (`proxy_headers=False` on both listeners); the app wraps itself in `ProxyHeadersMiddleware` with `CAM2SIP_TRUSTED_PROXIES`. Never use `forwarded_allow_ips="*"`: it makes the left-most, client-supplied `X-Forwarded-For` entry the client IP, which defeats the login throttle (`auth.LoginThrottle`).
- Tests pick the client address with `TestClient(app, client=(ip, port))`. The proxy middleware is inside the app, so tests cover `X-Forwarded-For` handling; `172.17.0.2` (Docker bridge) is trusted by the default `private` setting, a public address isn't.
- **Adaptive mic gain (`media/agc.py`) is tuned on real audio, not just tones.** The live C212 room showed that clicks/clinks pass a pure level+duration speech test and got +8 dB, and a steady fan fools any noise-floor tracker until it catches up. Hence the voicing check (< 3000 zero crossings/s; measured: vowels 1000–2600, room hiss ~2900, clicks 3700+) and the syllable rule (the gain rises only after 50 ms–1.5 s of voice ≥ 10 dB above the floor that is followed by a 6 dB dip). `tests/test_agc.py` has click, fan and espeak-speech cases. Mutating either rule makes them fail. Replay recordings (`/api/cameras/{id}/mic.wav` is unprocessed) through `Agc` to check changes.
- `SECURE_COOKIES=auto` marks the cookie `Secure` only for HTTPS reported by a trusted proxy, not for the direct :8443 listener: browsers don't let an `http://` page overwrite a `Secure` cookie, so it would break sign-in on :8090 for the same host.

## Release

Pushes to `main` run CI (pytest) and build a multi-arch image to `ghcr.io/revocx35/cam-to-sip` (`latest`, `sha-…`, and semver tags for `v*` tags). `docker-compose.yml` has both `build: .` and that image name.
