# Troubleshooting

Start with **Logs** in the web UI, or `docker logs -f cam2sip`. For SIP problems set `SIP_TRACE=true` in `.env` and run `docker compose up -d` to log every SIP message.

## Registration

| Symptom | Cause / fix |
|---|---|
| `failed: REGISTER: no response from x.x.x.x:5060` | PBX unreachable, wrong port, or the PBX firewall is blocking the host. FreePBX: add the cam2sip host to *Firewall → Networks → Trusted*, or check *Responsive Firewall*. |
| `failed: authentication rejected - check username/password` | wrong extension secret, or auth username differs from the extension (set *Auth username*) |
| `failed: 403 Forbidden` | extension doesn't exist / is disabled, or the PBX's ACL blocks the IP |
| Registered, but calls never arrive | the PBX sends INVITEs to a different address: set `ADVERTISE_IP` to the host's LAN IP, and check `asterisk -rx "pjsip show contacts"` shows `sip:c2s-…@<host-ip>:5062` |
| `address already in use` at start | another service uses UDP 5062 or the RTP range: change `SIP_PORT` / `RTP_PORT_*` |

## Calls

| Symptom | Cause / fix |
|---|---|
| Caller hears busy | the camera is already in a call (`486`), or the caller isn't in *Allowed callers* (`403`) |
| Caller hears "unavailable" | no enabled bridge for this phone, or camera disabled (`480`) |
| Call connects, silence both ways | RTP blocked: allow UDP `16000-16199` from the PBX; check the *RTP* line in the dashboard's call card (packets in/out) |
| Hear the camera, camera doesn't hear you | see *Speaker* below |
| Camera hears you, you hear nothing | see *Microphone* below |
| You hear the camera only when you're silent | expected with echo-cancelling cameras (Tapo): the mic is ducked while the speaker plays. Lower *speaker gain*, keep the *noise gate* on, raise the gate threshold (e.g. −40 dBFS) if your line is noisy |
| Choppy audio from the camera | network jitter to the camera. The buffer adapts after underruns; check *buffer* in the call card. Wi-Fi cameras with weak signal are the usual cause |
| Echo on the phone | camera speaker too loud next to its mic: lower *speaker gain* |
| Call drops after ~32 s | the PBX never got our 200 OK, or we never got the ACK (NAT/firewall on the SIP path) |
| Call drops after 60 s | no RTP received from the phone/PBX for 60 s (one-way RTP path) |

## Microphone (camera → phone)

1. Cameras page → **Listen 4s**. If that's silent or fails:
   - wrong camera username/password or stream path (Tapo: `stream1` / `stream2`),
   - Tapo: keep *Request video together with audio* on (Tapo sends no audio otherwise),
   - the camera's microphone is disabled in its app.
2. **Check** should show `mic PCMA/8000` (or PCMU). If it shows something else (AAC), go2rtc transcodes via ffmpeg, which is fine but adds latency.

## Speaker (phone → camera)

1. Cameras page → **Test speaker** plays a chime.
2. **Check** must show `speaker PCMA/8000` (or PCMU).
   - **Tapo** shows `401 Unauthorized`: wrong **cloud** password (not the camera account!). Enable *Tapo Lab → Third-Party Compatibility*. The password is for the TP-Link account that owns the camera.
   - **Tapo** shows "speaker needs cloud password": enter it in the camera form.
   - **ONVIF** shows "no speaker": the camera/stream has no backchannel. Enable audio output in the camera, or try another stream path.
3. During a call the call card shows *Camera speaker: connected / talking*. *talking* means audio is above the gate.

## IVR menu

| Symptom | Cause / fix |
|---|---|
| Menu is silent or just beeps | espeak-ng is missing (custom image): prompts fall back to beeps. The official image includes it. Check **Preview menu** in the bridge form |
| Wrong language / accent | Set *Voice / language* (e.g. `tr`, `de`) **and** write the prompt texts in that language |
| Key presses do nothing | The phone/PBX must send DTMF. FreePBX default *RFC 4733* works, as do SIP INFO and in-band tones. Check the logs for `DTMF` lines; if there are none, set the extension's DTMF mode to RFC 4733 |
| "… is busy right now" | That camera is in another call (SIP or browser) |
| Hangs up after the menu | No key pressed within *Repeat menu* × (menu length + *Wait after menu*) |
| `*` doesn't return to the menu | Only in IVR bridges, and only when `*` is the *back-to-menu digit* (and not the hang-up digit) |

## Browser calls

| Symptom | Cause / fix |
|---|---|
| "Browsers only allow the microphone on secure pages" | You opened the UI over `http://<ip>`. Use `https://<ip>:8443` (accept the self-signed certificate) or your HTTPS reverse proxy. `http://localhost` also counts as secure |
| Certificate warning on :8443 | Expected with the self-signed certificate: accept it once. For a trusted certificate, mount your own and set `TLS_CERT` / `TLS_KEY` |
| "Microphone unavailable (NotAllowedError)" | The browser blocked mic access for the site: allow it in the address-bar permissions |
| "Tap to start audio" | The browser blocked autoplay: tap it (or press the talk button) once |
| Call page connects, then "… is already in a call" | The camera is in a SIP call or another browser call |
| Video stays on "Connecting…" or shows slow snapshots | The browser lacks MSE/H.264 (it falls back to 1 fps snapshots), or the camera's mic stream has no video. Check *Request video together with audio* and the snapshot on the Cameras page |
| No audio / video through a reverse proxy | Enable WebSocket support on the proxy (Nginx Proxy Manager: *Websockets Support*; nginx: `proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";`) |
| Echo or feedback | Use push-to-talk or headphones. Lower "Your voice on the camera" |
| The camera doesn't hear you in open-mic mode | Your voice is below the noise gate (-55 dBFS): speak up, raise "Your voice on the camera", or use push-to-talk (not gated) |

## go2rtc

- Dashboard tile **go2rtc offline**: `docker logs cam2sip-go2rtc`. Check nothing else uses `127.0.0.1:11984` / `18554` (change `GO2RTC_API_PORT` / `GO2RTC_RTSP_PORT`).
- To inspect go2rtc directly from the host: `curl -s http://127.0.0.1:11984/api/streams | jq`.
- To use go2rtc's web UI temporarily, change its API listen address to `0.0.0.0:11984` in `docker-compose.yml`. Don't leave it exposed: it shows camera credentials.

## End-to-end test without a phone

```bash
docker build -t cam2sip-dev -f Dockerfile.dev .
docker run --rm --network host -v $PWD:/app -w /app cam2sip-dev \
  python tools/sip_test_call.py --server <pbx-ip> --user <some-ext> --password <secret> \
  --target <bridged-ext> --wav /app/rx.wav
```

It calls the bridged extension through the PBX, sends a tone (you'll hear it on the camera), records the camera audio into `rx.wav` and prints RTP statistics. Asterisk even lets you use the bridged extension's own credentials as the caller.
