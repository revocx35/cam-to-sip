# REST API

Base URL: `http://<server>:8090/api`. Interactive OpenAPI docs are at `/api/docs`.

## Authentication

- **Browser**: `POST /api/login {"password": "…"}` sets an HTTP-only session cookie (7 days).
- **Automations**: send `Authorization: Bearer <api-token>`. The token is under **Settings → Automation API** and can be regenerated there.

Public endpoints: `GET /api/health`, `GET /api/session` (also returns `https_port`), `POST /api/login`, `POST /api/logout`, `POST /api/setup` (first run only).

Secret fields (`password`, `cloud_password`) are never returned. Responses contain `""` plus a `<field>_set: true/false` flag. When updating, omit the field or send `""` to keep the stored value.

## Endpoints

### Status & logs

| Method | Path | Description |
|---|---|---|
| GET | `/status` | go2rtc state, per-phone registration, active calls with media stats, busy cameras |
| GET | `/logs?after=<seq>` | log records newer than `seq` (ring buffer, ~1500 entries) |
| GET | `/calls` | `{"active": [...], "history": [...]}` (last 200 calls) |
| POST | `/calls/{call_id}/hangup` | hang up an active call |

### Cameras

| Method | Path | Description |
|---|---|---|
| GET | `/cameras` | list (secrets redacted, `busy` flag) |
| POST | `/cameras` | create |
| PUT | `/cameras/{id}` | update (partial fields allowed) |
| DELETE | `/cameras/{id}` | delete (`409` while used by a bridge) |
| POST | `/cameras/probe` | test a camera config, saved (`{"id": …}`) or unsaved (full body) → `{"mic", "speaker", "video", "errors"}` |
| POST | `/cameras/onvif-discover` | `{"host","port","username","password"}` → device info + profiles with RTSP URIs |
| GET | `/cameras/{id}/snapshot.jpg` | JPEG snapshot |
| GET | `/cameras/{id}/mic.wav?seconds=4` | record the microphone (1–15 s) |
| POST | `/cameras/{id}/test-speaker` | play a chime on the speaker |
| POST | `/cameras/{id}/test-notice` | play the call notice on the speaker; body may override `notify_text`, `notify_sound`, `notify_voice`, `notify_speed` |

Camera object:

```json
{
  "name": "Front door",
  "kind": "tapo",               // "tapo" | "onvif" | "custom"
  "enabled": true,
  "host": "192.168.1.50",
  "rtsp_port": 554,
  "onvif_port": 2020,
  "username": "camuser",
  "password": "…",              // write-only
  "stream_path": "stream2",
  "cloud_password": "…",        // tapo only, write-only
  "listen_url": "",             // custom only: go2rtc source for the mic
  "talk_url": "",               // custom only: go2rtc source for the speaker
  "mic_with_video": true,
  "notify_enabled": false,      // privacy call notice on the camera speaker
  "notify_text": "Attention please. A call has started on this camera.",
  "notify_sound": "",           // uploaded sound id; overrides notify_text
  "notify_voice": "en-us",
  "notify_speed": 150
}
```

### Virtual phones

| Method | Path | Description |
|---|---|---|
| GET | `/phones` | list with `status` (`registered`, `registering`, `failed` + `error`) |
| POST | `/phones` | create (starts registering immediately) |
| PUT | `/phones/{id}` | update (re-registers) |
| DELETE | `/phones/{id}` | delete (unregisters; `409` while used by a bridge) |
| POST | `/phones/{id}/register` | force a registration refresh |

```json
{
  "name": "Front door intercom",
  "enabled": true,
  "server": "192.168.1.10",
  "port": 5060,
  "username": "1008",
  "password": "…",
  "auth_username": "",          // optional, defaults to username
  "domain": "",                 // optional, defaults to server
  "display_name": "Front door",
  "expires": 300
}
```

### Bridges

| Method | Path | Description |
|---|---|---|
| GET | `/bridges` | list (`busy` = camera in a call) |
| POST | `/bridges` | create (`409` if the phone is already bridged) |
| PUT | `/bridges/{id}` | update |
| DELETE | `/bridges/{id}` | delete |
| POST | `/bridges/{id}/call` | `{"target": "600", "camera_id": "…"}`: the camera calls a number or SIP URI → `{"call_id"}`. `camera_id` is optional (IVR bridges: which menu camera; default the first). `409` if the camera is busy or the phone isn't registered |
| GET | `/ivr/voices` | `{"available": true, "voices": [{"id": "en-us", "name": "English (America)"}, …]}` (espeak-ng voices) |
| POST | `/ivr/preview` | bridge-like body (`ivr_options`, `ivr_greeting`, `ivr_option_text`, `ivr_voice`, `ivr_speed`) or `{"text": "…", "ivr_voice": "tr"}` → `audio/wav` of the spoken menu |

```json
{
  "name": "Front door",
  "enabled": true,
  "mode": "direct",             // "direct" (one camera) or "ivr" (spoken menu of cameras)
  "camera_id": "a1b2c3d4",      // direct mode
  "phone_id": "e5f6a7b8",
  "answer_delay": 0,            // seconds of ringing before auto-answer
  "mic_gain_db": 0,             // camera -> phone
  "speaker_gain_db": 0,         // phone -> camera
  "speaker_gate_db": -50,       // null = gate off
  "max_call_seconds": 600,
  "allowed_callers": ["1001"],  // empty = anyone
  "hangup_digit": "#",
  "dtmf_actions": [
    {"digit": "1", "method": "POST", "url": "http://ha:8123/api/webhook/open-gate", "body": "", "hangup": true}
  ]
}
```

IVR bridge (`mode: "ivr"`), in addition to the common fields above:

```json
{
  "mode": "ivr",
  "ivr_options": [
    {"digit": "1", "camera_id": "a1b2c3d4", "label": ""},            // label = spoken name, default camera name
    {"digit": "2", "camera_id": "c9d8e7f6", "label": "the garage", "sound": ""}   // sound replaces the spoken line
  ],
  "ivr_greeting": "Hello.",
  "ivr_option_text": "Press {digit} for {name}.",
  "ivr_invalid_text": "Sorry, that is not a valid choice.",
  "ivr_busy_text": "{name} is busy right now.",
  "ivr_connect_text": "Connecting to {name}.",   // "" = connect silently
  "ivr_goodbye_text": "Goodbye.",
  "ivr_voice": "en-us",                          // any espeak-ng voice: en-gb, de, fr, tr, …
  "ivr_speed": 150,                              // words per minute, 80-300
  "ivr_timeout": 8,                              // seconds to wait after the menu
  "ivr_repeats": 3,                              // menu repetitions before hanging up
  "menu_digit": "*",                             // during a camera call: back to the menu
  "ivr_greeting_sound": "",                      // uploaded sound ids replacing the texts above
  "ivr_invalid_sound": "", "ivr_busy_sound": "", "ivr_connect_sound": "", "ivr_goodbye_sound": ""
}
```

Menu digits are `0`-`9` and must be unique; `menu_digit` must not be one of them.

### Browser calls (WebSockets)

These are used by the web UI's call page. They authenticate with the session cookie (bearer tokens can't be sent by browsers on WebSockets), and they're served on both the HTTP and the HTTPS port.

| Path | Direction | Frames |
|---|---|---|
| `WS /cameras/{id}/talk` | both | **binary**: G.711 A-law, 8 kHz mono. Server → browser is camera mic audio; browser → server is audio for the camera speaker (20 ms frames). **text** (server → browser): `{"type":"status","mic":{...},"speaker":{...},"duration":12}` every second, `{"type":"error","message":...}`, `{"type":"ended","reason":...}`. **text** (browser → server): `{"type":"gate","db":-55}` (open-mic noise gate; `null` = send everything, for push-to-talk), `{"type":"hangup"}` |
| `WS /cameras/{id}/video` | both | go2rtc's MSE protocol for this camera only. Send `{"type":"mse","value":"avc1.640029,..."}`; you receive `{"type":"mse","value":"video/mp4; codecs=..."}` followed by binary fMP4 segments |

A talk connection gets `{"type":"error","message":"… is already in a call"}` when the camera is busy (SIP or another browser). Active browser calls show up in `/status` and `/calls` with `"direction": "web"`, and `POST /calls/{id}/hangup` ends them.

### Sounds

| Method | Path | Description |
|---|---|---|
| GET | `/sounds` | `[{"id","name","duration","created","used_by":[...]}]` |
| POST | `/sounds?name=<name>` | body: a PCM **WAV** file (`Content-Type: audio/wav`, any rate/channels, max 120 s). It is converted to 8 kHz mono. The web UI converts other formats in the browser first |
| GET | `/sounds/{id}.wav` | the stored 8 kHz WAV |
| PUT | `/sounds/{id}` | `{"name": "…"}`: rename |
| DELETE | `/sounds/{id}` | delete (`409` while a camera notice or bridge prompt uses it) |

`/ivr/preview` also accepts `{"sound": "<id>"}` or `{"text": "…"}` for a single prompt, and uploaded sounds in `ivr_greeting_sound` / `ivr_options[].sound`.

```bash
curl -X POST "http://cam2sip:8090/api/sounds?name=Door%20chime" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: audio/wav" --data-binary @chime.wav
```

### Settings

| Method | Path | Description |
|---|---|---|
| GET | `/settings` | version, ports, API token |
| POST | `/settings/password` | `{"current", "new"}` |
| POST | `/settings/api-token` | regenerate the token |

## Automation examples

**curl**

```bash
TOKEN=...; BRIDGE=...
curl -fsS -X POST "http://cam2sip:8090/api/bridges/$BRIDGE/call" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"target":"600"}'
```

**Home Assistant**: doorbell button → ring group 600, with the camera as intercom:

```yaml
# configuration.yaml
rest_command:
  cam2sip_front_door:
    url: "http://cam2sip:8090/api/bridges/<bridge-id>/call"
    method: POST
    headers:
      Authorization: !secret cam2sip_bearer   # "Bearer <token>"
    content_type: "application/json"
    payload: '{"target": "600"}'

# automation
automation:
  - alias: Doorbell calls desk phones
    trigger:
      - platform: state
        entity_id: binary_sensor.doorbell_button
        to: "on"
    action:
      - service: rest_command.cam2sip_front_door
```

**Frigate** (through Home Assistant): trigger the same `rest_command` from a `frigate/events` MQTT automation, e.g. a `person` entering the `porch` zone.

**DTMF → Home Assistant webhook**: add a DTMF action `1 → POST http://homeassistant:8123/api/webhook/<id>` to a bridge. Pressing 1 during a call then fires the webhook, e.g. to open a gate relay.
