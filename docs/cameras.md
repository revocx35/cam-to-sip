# Cameras

cam2sip needs two things from a camera:

| Direction | What | How cam2sip gets it |
|---|---|---|
| **Microphone** (camera → caller) | an audio track | RTSP via go2rtc |
| **Speaker** (caller → camera) | a two-way-audio *backchannel* | go2rtc: `tapo://`, ONVIF RTSP backchannel, or other go2rtc backchannels |

Use **Test connection** in the camera form (or **Check** on the camera card) to see what the camera offers. You want both lines:

```
Microphone: PCMA/8000     Speaker: PCMA/8000
```

![Camera form with connection test](images/camera-form.png)

A camera without a speaker still works for listening only. A camera whose mic isn't G.711 (e.g. AAC) is transcoded on the fly by go2rtc's ffmpeg.

## TP-Link Tapo (C100/C110/C2xx/C3xx/C5xx…)

Tested: **Tapo C212**, firmware 1.5.1.

| Field | Value |
|---|---|
| Type | TP-Link Tapo |
| Host / IP | camera IP (give it a DHCP reservation) |
| Camera username / password | the **camera account**: Tapo app → camera → ⚙ → *Advanced settings* → *Camera account* |
| Stream path | `stream2` (SD, recommended) or `stream1` (HD). Audio is the same. |
| TP-Link cloud password | the password of your **Tapo app / TP-Link ID**; only the password, not the e-mail |
| Request video together with audio | ✅ keep enabled |

Why two passwords?

- The **camera account** works for RTSP/ONVIF: video and **microphone**.
- Talking through the **speaker** only works over Tapo's own protocol (port 8800). The camera authenticates that as user `admin` with your **cloud password**. The camera account is rejected there (`401 Unauthorized`).

If the speaker still fails with the correct cloud password:

- Newer firmware requires *Tapo app → Me → Tapo Lab → **Third-Party Compatibility** → On*.
- Changing the TP-Link password means updating it here too.

Behaviour to know:

- Tapo streams audio only while video is also streaming. cam2sip asks go2rtc for video and discards it (`Request video together with audio`).
- Tapo uses echo cancellation: the **mic is ducked while the speaker plays**. Keep the bridge's *noise gate* enabled so background noise from the phone line doesn't keep the camera deaf.
- Tapo delivers audio in ~128 ms chunks, which adds ~150 ms of buffering on the way to the phone.
- If Frigate/Home Assistant also use `tapo://` two-way audio on the same camera, only one talk session may work at a time.

### Using the same camera with Frigate

cam2sip opens its own connections only **during calls**, plus probes and snapshots. It doesn't need Frigate's go2rtc. You can point cam2sip at Frigate's restream instead of the camera by choosing *Custom* with `rtsp://frigate-host:8554/c212_2` as the microphone source and `tapo://<cloud password>@<camera ip>?subtype=1` as the speaker source.

## ONVIF Profile T cameras (RTSP audio backchannel)

Many Hikvision, Dahua/Amcrest, Reolink, Axis, Uniview… cameras with a speaker or line-out support an ONVIF **audio backchannel** on RTSP.

1. Type: **ONVIF / RTSP (backchannel)**.
2. Enter host, camera username/password and ONVIF port (often `80`, `8000` or `8080`).
3. Click **Discover streams (ONVIF)** and **Use** a profile. This fills the RTSP port and path.
4. **Test connection** shows `Speaker: PCMU/8000` (or PCMA) if the backchannel is available.

If the speaker shows *not available*:

- Enable two-way audio / audio output in the camera's web UI (Hikvision: *Audio → Audio Output*; Dahua: *Audio → enable*).
- Some cameras only allow one backchannel client at a time; close other apps (NVR, vendor app).
- Some cameras need the backchannel on a specific stream path. Try the main stream.
- Hikvision's ISAPI two-way audio isn't ONVIF. Try the ONVIF user with `rtsp://…/Streaming/Channels/101`.

## Custom go2rtc sources

Type **Custom go2rtc source** accepts anything [go2rtc supports](https://github.com/AlexxIT/go2rtc#module-streams):

| Example | Mic | Speaker |
|---|---|---|
| RTSP with ONVIF backchannel | `rtsp://user:pass@ip:554/stream` | same URL |
| Tapo | `rtsp://…/stream2` | `tapo://cloudpass@ip` |
| Dahua/others via DVRIP | `dvrip://user:pass@ip:34567?channel=0&subtype=0` | same URL |
| Ring, Kasa, Xiaomi… | per go2rtc docs | per go2rtc docs |
| Any camera (listen only) | `rtsp://…` | *(empty)* |

The speaker source must expose a `sendonly` audio track in go2rtc's probe (that is the backchannel). **Test connection** tells you.

## Diagnostics from the UI

| Button | What it does |
|---|---|
| **Call** | opens a browser call: live video, camera audio, push-to-talk (see the README's *Browser calls*) |
| **Check** | probes mic and speaker through go2rtc and shows the codecs |
| **Listen 4s** | records 4 s from the camera mic and plays it in your browser |
| **Test speaker** | plays a two-tone chime through the camera speaker, over the same path calls use |
| Snapshot | live JPEG from go2rtc (needs video in the mic source) |
