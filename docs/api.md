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
  "mic_with_video": true
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
| POST | `/bridges/{id}/call` | `{"target": "600"}`: the camera calls a number or SIP URI → `{"call_id"}` (`409` if the camera is busy or the phone isn't registered) |

```json
{
  "name": "Front door",
  "enabled": true,
  "camera_id": "2c08d7c4",
  "phone_id": "b2532b87",
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

### Browser calls (WebSockets)

These are used by the web UI's call page. They authenticate with the session cookie (bearer tokens can't be sent by browsers on WebSockets), and they're served on both the HTTP and the HTTPS port.

| Path | Direction | Frames |
|---|---|---|
| `WS /cameras/{id}/talk` | both | **binary**: G.711 A-law, 8 kHz mono. Server → browser is camera mic audio; browser → server is audio for the camera speaker (20 ms frames). **text** (server → browser): `{"type":"status","mic":{...},"speaker":{...},"duration":12}` every second, `{"type":"error","message":...}`, `{"type":"ended","reason":...}`. **text** (browser → server): `{"type":"gate","db":-55}` (open-mic noise gate; `null` = send everything, for push-to-talk), `{"type":"hangup"}` |
| `WS /cameras/{id}/video` | both | go2rtc's MSE protocol for this camera only. Send `{"type":"mse","value":"avc1.640029,..."}`; you receive `{"type":"mse","value":"video/mp4; codecs=..."}` followed by binary fMP4 segments |

A talk connection gets `{"type":"error","message":"… is already in a call"}` when the camera is busy (SIP or another browser). Active browser calls show up in `/status` and `/calls` with `"direction": "web"`, and `POST /calls/{id}/hangup` ends them.

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
