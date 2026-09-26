# Architecture

cam2sip joins two worlds:

- **Telephony**: SIP signalling plus RTP audio (G.711) with a PBX such as FreePBX/Asterisk.
- **Cameras**: RTSP streams, ONVIF backchannels and vendor protocols such as Tapo's talk-back.

The camera side is delegated to **go2rtc**, which already speaks every camera dialect. cam2sip's own process implements a small SIP user agent, the audio bridge and the web UI. The web UI can also **call a camera from the browser** over WebSockets (section 7a).

## 1. Runtime components

```mermaid
flowchart LR
    phone["IP phone / softphone"] -- "SIP + RTP" --> pbx["PBX<br/>(FreePBX / Asterisk)"]
    pbx -- "SIP (UDP :5062)<br/>RTP (UDP 16000-16199)" --> app

    subgraph host["Docker host (network_mode: host)"]
      subgraph app["cam2sip container (Python / asyncio)"]
        ua["SIP UA<br/>sip/*"]
        engine["Engine + BridgeSession<br/>engine.py"]
        rtsp_c["RtspAudioClient<br/>media/rtsp.py"]
        talk["TalkServer :18555<br/>media/rtsp.py"]
        web["FastAPI + SPA :8090 / :8443<br/>web/* + webcall.py"]
      end
      subgraph g2r["go2rtc container"]
        api["HTTP API :11984"]
        rtsp_s["RTSP server :18554"]
      end
    end

    ua <--> engine
    engine --> rtsp_c -- "RTSP pull (mic)" --> rtsp_s
    engine --> talk
    g2r -- "RTSP pull (speaker audio)" --> talk
    engine -- "streams / play / probe / snapshot" --> api
    g2r -- "RTSP / tapo:// / ONVIF backchannel" --> cam["IP camera"]
    browser["Browser"] -- "HTTP(S) + WebSockets<br/>(UI, call audio, MSE video)" --> web
```

| Container | Image | Listens on | Role |
|---|---|---|---|
| `cam2sip` | built from this repo | `0.0.0.0:8090/tcp` (UI/API), `0.0.0.0:8443/tcp` (same over TLS), `0.0.0.0:5062/udp` (SIP), `16000-16199/udp` (RTP), `127.0.0.1:18555/tcp` (talk RTSP) | SIP UA, bridge engine, web UI, browser calls |
| `cam2sip-go2rtc` | `alexxit/go2rtc:1.9.14` | `127.0.0.1:11984` (API), `127.0.0.1:18554` (RTSP) | camera protocols, backchannel, snapshots, transcoding |

Both containers use **host networking**:

- SIP/SDP must carry an address the PBX can reach.
- RTP uses a port range that Docker would otherwise have to proxy port by port.
- The two containers talk over `127.0.0.1`.

go2rtc runs from an **inline JSON config with no config file**. It is stateless, and cam2sip pushes streams into it through the API (section 5).

## 2. Code map

```
cam2sip/
  __main__.py        entry point (uvicorn)
  settings.py        CAM2SIP_* environment variables
  models.py          pydantic models: Camera, Phone, Bridge, Settings, Config (+ go2rtc source building)
  store.py           JSON persistence (/data/config.json, call_history.json), atomic writes, 0600
  engine.py          Engine (lifecycle, config -> go2rtc/SIP, call routing, camera tools)
                     CameraLink (camera side of any call: mic in, speaker out, gate, keep-alive)
                     BridgeSession (SIP call <-> CameraLink: pacer, DTMF, watchdog)
  webcall.py         WebCall (browser call over a WebSocket <-> CameraLink)
  go2rtc.py          go2rtc API client + stream reconciliation + probe parsing
  onvif.py           minimal ONVIF SOAP client (device info, profiles, stream URIs)
  logbuffer.py       in-memory log ring buffer for the UI
  sip/
    message.py       SIP message parser/serializer, URI / name-addr / Via helpers
    auth.py          Digest auth (MD5, SHA-256, qop=auth/auth-int)
    sdp.py           SDP parse/build, G.711 codec negotiation
    stack.py         UDP transport + client/server transactions (retransmissions, CANCEL, ACK)
    ua.py            UserAgent, Account (REGISTER loop), Call (dialog: UAS + UAC)
  media/
    g711.py          A-law/mu-law tables, translate+gain tables, level meter, tone, WAV
    rtp.py           RTP packets, port allocator, RtpEndpoint (socket), Pacer (jitter buffer)
    rtsp.py          RtspAudioClient (pull mic from go2rtc), TalkServer/TalkSession (serve speaker audio)
  web/
    app.py           FastAPI app: auth middleware, REST API, static UI
    auth.py          scrypt password hash, HMAC-signed session cookie
    tls.py           self-signed certificate for the HTTPS listener (openssl)
    static/          single-page UI (vanilla JS, no build step)
      app.js         pages, router, forms
      call.js        CameraCall: browser audio (A-law over WebSocket) + MSE video
      mic-worklet.js AudioWorklet: mic -> 8 kHz frames (low-pass + resample)
tests/               pytest: SIP parsing/auth, SDP, G.711, pacer, SIP loopback calls, web API
tools/sip_test_call.py      live end-to-end SIP call tester (through a real PBX)
tools/browser_call_test.py  headless Firefox smoke test of a browser call
```

## 3. Call flows

### Inbound: phone calls the virtual extension

```mermaid
sequenceDiagram
    participant P as IP phone
    participant X as PBX
    participant U as cam2sip UA
    participant E as Engine
    participant G as go2rtc
    participant C as Camera

    Note over U,X: REGISTER (digest) every expires*0.75, Contact sip:c2s-xxxx@host:5062
    P->>X: INVITE 1008
    X->>U: INVITE sip:c2s-xxxx@host:5062 (SDP offer)
    U-->>X: 100 Trying
    U->>E: on_incoming(call)
    E->>E: find bridge, check enabled / caller whitelist / camera busy
    E-->>X: 180 Ringing
    par connect camera audio while ringing
        E->>G: RTSP DESCRIBE/SETUP/PLAY c2s_<cam>?video&audio=pcma,pcmu
        G->>C: RTSP (or tapo://) pull
    and
        E->>G: POST /api/streams?dst=c2s_<cam>_talk&src=rtsp://127.0.0.1:18555/talk/<token>
        G->>C: open backchannel (tapo talk session / ONVIF backchannel)
        G->>E: RTSP DESCRIBE/SETUP/PLAY /talk/<token> (pull speaker audio)
    end
    Note over E: wait bridge.answer_delay
    E-->>X: 200 OK (SDP answer: PCMA or PCMU + telephone-event)
    X->>U: ACK
    Note over P,C: audio flows both ways (section 4)
    P->>X: hang up
    X->>U: BYE
    U-->>X: 200 OK
    E->>G: close talk RTSP + POST dst with empty src (stop)
```

Routing: the Request-URI user is the account's unique `contact_user` (`c2s-xxxxxxxx`), so several virtual phones can share one UDP port, even when they register to different PBXs. If that doesn't match, cam2sip falls back to the `To` user.

Rejections:

| Condition | Response |
|---|---|
| No bridge, or bridge/camera disabled | `480 Temporarily Unavailable` |
| Caller not on the whitelist | `403 Forbidden` |
| Camera already in a call | `486 Busy Here` |
| No common codec | `488 Not Acceptable Here` |
| Caller hangs up while ringing (CANCEL) | `487 Request Terminated` |

### Outbound: camera calls a phone (doorbell)

`POST /api/bridges/{id}/call {"target": "600"}` leads to `Account.dial()`:

1. cam2sip sends an INVITE with an SDP offer (PCMA, PCMU, telephone-event/101).
2. If the PBX answers `401`/`407`, cam2sip retries once with digest auth (CSeq+1, new branch).
3. On `2xx`, it sends the ACK and the call becomes active.

The BridgeSession starts at dial time, so camera audio is ready when the callee answers. Unanswered calls are CANCELled after 60 s.

## 4. Media pipeline

All audio is **G.711 at 8 kHz** (A-law or μ-law). Each G.711 byte is one sample, so converting between A-law and μ-law and applying a gain is a single `bytes.translate()` with a cached 256-byte table (`g711.translate_table`). No per-sample Python loops run on the hot path.

### Camera → phone (downlink)

```
camera ──RTSP──► go2rtc ──RTSP/TCP interleaved──► RtspAudioClient
                                                   │  (payload bytes, PCMA/PCMU)
                                                   ▼
                                    g711.convert(cam codec → call codec, mic gain)
                                                   ▼
                                    Pacer (adaptive jitter buffer, 20 ms clock)
                                                   ▼
                                    RtpEndpoint.send()  ──RTP/UDP──► PBX ──► phone
```

- The client asks go2rtc for `c2s_<cam>?video&audio=pcma,pcmu` but only SETUPs the audio track.
  - **Tapo quirk:** the camera sends *nothing* when only its audio track is set up. Asking go2rtc for video makes it pull video from the camera; go2rtc then drops the video because we never SETUP that track. Controlled by `Camera.mic_with_video`.
- go2rtc re-labels payload types (PCMA arrives as PT 96), so the client reads the codec from the SDP `rtpmap`.
- Tapo delivers audio in 1024-byte chunks (128 ms) every 80–160 ms. The `Pacer`:
  - starts playing when it holds the largest chunk seen plus a 40 ms margin,
  - trims the backlog at start,
  - sends silence on underrun and grows the margin by 20 ms (up to 300 ms),
  - drops the oldest audio when more than 240 ms above target.

  Typical latency added: ~150 ms.
- The mic stream in go2rtc has a second source, `ffmpeg:c2s_<cam>#audio=pcma`. go2rtc only starts that ffmpeg transcoder if the camera's native codec isn't G.711 (e.g. AAC).

### Phone → camera (uplink)

```
phone ──RTP──► RtpEndpoint.on_packet
                  │ (DTMF PT → _rtp_dtmf)
                  ▼
         noise gate (level_dbfs ≥ threshold, 600 ms hangover)
                  ▼
         g711.convert(call codec → camera talk codec, speaker gain)
                  ▼
         TalkSession.push()  ──RTSP interleaved──►  go2rtc  ──► tapo:// talk / ONVIF backchannel ──► camera speaker
```

- go2rtc can only *play into* a backchannel from a source it pulls itself (`POST /api/streams?dst=<stream>&src=<url>`). So cam2sip runs a tiny **RTSP server** (`TalkServer`, 127.0.0.1:18555). Each call gets a random token URL.
- That URL offers two tracks, PCMA (`trackID=0`) and PCMU (`trackID=1`). go2rtc SETUPs whichever one the camera's backchannel accepts, and that tells us the codec.
- go2rtc drops an RTSP source after **5 s without data**. While the gate is closed, cam2sip sends one silence frame per second as a keep-alive, and advances the RTP timestamp by the real elapsed time (`TalkSession.advance`).
- **Echo cancellation / half duplex:** Tapo opens its talk session in `aec` mode and ducks its mic while the speaker plays (about −10 dB). The noise gate (default −50 dBFS) keeps line noise from triggering that, so you can hear the camera when nobody on the phone is speaking.

### DTMF

RFC 4733 telephone-events (deduplicated per RTP timestamp) and SIP INFO (`application/dtmf-relay`) both reach `BridgeSession._on_dtmf`. That handles the hang-up digit and the HTTP webhook actions.

### CameraLink: the shared camera side

`engine.CameraLink` owns everything that talks to the camera during any call:

- the `RtspAudioClient` (mic),
- the `TalkSession` and the go2rtc play/re-attach loop (speaker),
- the noise gate (`speak(payload, codec, gain, gate)`),
- the 1 s silence keep-alive.

`BridgeSession` (SIP) and `WebCall` (browser) are thin adapters around it. A camera is "busy" while any of them holds it (`Engine.busy`).

## 5. go2rtc integration

| Stream name | Sources | Used for |
|---|---|---|
| `c2s_<camera id>` | `<mic source>`, `ffmpeg:c2s_<id>#audio=pcma` | microphone, snapshots (`/api/frame.jpeg`) |
| `c2s_<camera id>_talk` | `<talk source>` | speaker (backchannel) via `POST /api/streams?dst=…` |
| `c2s_probe_*` | temporary | "Test connection" probes (deleted afterwards) |

Sources per camera type (`models.Camera`):

| kind | mic source | talk source |
|---|---|---|
| `tapo` | `rtsp://user:pass@host:554/stream2` | `tapo://<cloud password>@host?subtype=1` (only if a cloud password is set) |
| `onvif` | `rtsp://user:pass@host:port/<path>` | same URL; go2rtc sends `Require: www.onvif.org/ver20/backchannel` |
| `custom` | any go2rtc source | any go2rtc source with a backchannel |

**Reconciliation** (`Go2rtc.reconcile`, every 15 s and on every config change):

1. cam2sip builds the desired `c2s_*` streams from the config.
2. It `PUT`s missing or changed streams and `DELETE`s unknown `c2s_*` ones.
3. If go2rtc restarts, it comes back empty and is repopulated within 15 s.

`PUT` answers `400 config file disabled` because go2rtc runs without a config file. The stream is still created, so that error is ignored.

Probing (`GET /api/streams?src=X&audio=all&video=all&microphone`) makes go2rtc connect and list the media. `recvonly` audio means a mic; `sendonly` audio means a speaker (backchannel).

## 6. SIP stack scope

Implemented (RFC 3261 subset, enough for Asterisk/FreePBX and similar PBXs):

- UDP transport, one socket (port 5062) shared by all accounts.
- Client transactions with retransmission timers (A/B, E/F), ACK for non-2xx, a 32 s linger to absorb retransmitted responses.
- Server transaction cache: retransmitted requests get the last response again; CANCEL is matched to its INVITE.
- REGISTER with digest auth (MD5/SHA-256, `qop=auth`), `423 Min-Expires`, stale nonces, refresh at 75 % of the granted expiry, exponential backoff on failure, unregister on shutdown.
- Dialogs:
  - UAS: 100/180/200, 200 OK retransmission until ACK (timer G/H), re-INVITE/UPDATE (hold, codec change), BYE, INFO, OPTIONS.
  - UAC: INVITE with auth, early dialog, CANCEL, re-ACK of retransmitted 2xx, BYE.
- Record-Route / Route sets, `rport`, symmetric RTP (latch onto the real RTP source).

Not implemented, by design for this use case:

- TCP/TLS, SRTP, ICE, video.
- Codecs other than G.711.
- Session timers, 100rel/PRACK.
- DNS SRV/NAPTR (A records only).
- Authentication of inbound requests.

The PBX handles transcoding and security at the edge.

## 7. Web UI & API

- FastAPI serves `/api/*` and the static SPA (`/`, `/static/*`). Interactive docs are at `/api/docs`.
- Auth middleware protects every `/api/*` route except `health`, `session`, `login`, `logout` and `setup`. It accepts either:
  - an HMAC-signed session cookie bound to the password hash, so changing the password logs out old sessions, or
  - `Authorization: Bearer <api_token>`.
- Secrets (`password`, `cloud_password`) are write-only. The API returns `""` plus `<field>_set: true`, and an empty value on update keeps the stored one.
- The UI polls `/api/status` every 2–3 s and `/api/logs?after=<seq>` every 1.5 s on the logs page. It needs no WebSockets, so it works behind any proxy.

## 7a. Browser calls

```
browser mic ──getUserMedia──► AudioWorklet (low-pass 3.4 kHz, resample → 8 kHz, 20 ms)   [ScriptProcessor fallback]
            ──A-law encode (JS)──► WebSocket /api/cameras/{id}/talk ──► WebCall ──► CameraLink.speak() ──► camera speaker
camera mic ──► CameraLink ──► A-law ──► WebSocket (binary) ──► decode + upsample ──► scheduled AudioBuffers (200 ms playout)
camera video ──► go2rtc MSE ──► WebSocket proxy /api/cameras/{id}/video ──► MediaSource (fMP4 H.264) ──► <video>
```

- **Transport is a WebSocket through the app itself**, not WebRTC. There are no extra ports or ICE, it works through any HTTPS reverse proxy, go2rtc stays on localhost, and calls reuse `CameraLink` (gate, keep-alive, busy handling, history).
- **Audio format:** G.711 A-law, 8 kHz, both ways (64 kbit/s each way). It's the camera's native codec, and the browser does the tiny encode/decode itself.
- **Talk protocol** (`webcall.py` docstring):
  - binary frames carry audio;
  - the server sends JSON `status` every second, plus `error`/`ended`;
  - the browser sends JSON `{"type":"gate","db":-55|null}` (open mic vs push-to-talk) and `hangup`.
- **Video**: the app proxies go2rtc's `/api/ws?src=c2s_<id>` for that one stream and forwards only `{"type":"mse"}` requests. The browser appends fMP4 segments to a `SourceBuffer` (or `ManagedMediaSource` on Safari) and seeks to stay within ~0.2 s of live. Without MSE/H.264 it falls back to 1 fps JPEG snapshots.
- **Secure context**: `getUserMedia` and AudioWorklets only exist on HTTPS pages (or localhost). The app therefore runs a second uvicorn listener with TLS on `HTTPS_PORT` (default 8443) inside the same process. That listener starts in the FastAPI lifespan with `lifespan="off"`, and signal handling is left to the main server. A self-signed certificate is generated in `/data/tls` unless `TLS_CERT`/`TLS_KEY` are set. On plain HTTP the call page is listen-only and links to the HTTPS URL.
- **Auth**: FastAPI HTTP middleware doesn't run for WebSockets, so both endpoints check the session cookie themselves. The cookie is `SameSite=strict`, which blocks cross-site WebSocket hijacking.
- **Echo / half duplex**: browser `echoCancellation` and `noiseSuppression` are on. Push-to-talk is the default because cameras duck their mic while the speaker plays.

## 8. Design decisions

| Decision | Why |
|---|---|
| go2rtc for cameras | It already supports RTSP, ONVIF backchannel, the Tapo talk protocol (with its encryption), DVRIP, Ring, Kasa and more, plus snapshots and ffmpeg transcoding. Re-implementing the Tapo talk protocol alone would be fragile. |
| Own SIP stack instead of Asterisk/PJSIP | Much smaller image, no C build, and full control over routing and media hooks. The feature set needed (register, answer, dial, G.711) is small and well tested against FreePBX 17. |
| G.711 only | Cameras speak G.711 (or get transcoded by go2rtc). Every PBX supports it, and gain/transcode becomes a table lookup. |
| Host networking | SIP/RTP behind Docker NAT needs port-range proxying and address rewriting, which is fragile. Host networking just works. |
| RTSP talk server (not publish) | go2rtc *pulls* from us straight into the camera consumer: one hop, and the call lifetime is tied to the TCP connection. |
| Browser calls over WebSocket, not WebRTC | Same port and proxy path as the UI, go2rtc stays private, and the camera side is shared with SIP calls. The cost is TCP instead of UDP for audio, which is fine on a LAN or over a decent uplink. |
| JSON file store | The data set is tiny and human-readable. Writes are atomic and there's nothing to migrate. |

## 9. Known limitations / ideas

- One active call per camera; a second caller gets busy (no multi-party mixing).
- Tapo talk-back needs the TP-Link cloud password, and on newer firmware *Third-Party Compatibility* enabled.
- Latency is roughly 150–300 ms end to end (camera chunking plus buffers). Fine for an intercom, not for music.
- Browser calls need HTTPS for the microphone (self-signed on :8443 by default).
- Ideas: SIP over TCP/TLS, G.722 wideband, video for SIP video phones, MQTT events, multiple cameras per phone (IVR digit selection), WebRTC for browser calls over high-latency links.
