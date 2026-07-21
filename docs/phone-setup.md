# Phone setup — bonded SRTLA ingest

You don't build anything on the phone: use an existing IRL streaming app that
speaks SRTLA (BELABOX's bonding protocol). Recommended:

| Platform | App | Notes |
|---|---|---|
| iOS | **Moblin** (free, open source) | First-class SRTLA + multi-link bonding |
| Android | **IRL Pro** | SRTLA supported, popular with IRL streamers |
| Android | **BELABOX** (with hardware encoder rig) | The reference SRTLA implementation |

## Connection settings

- **URL / server**: `srtla://<server-ip>:5000`
  (in apps that ask for host+port separately: host `<server-ip>`, port `5000`,
  protocol SRTLA)
- **Stream ID**: `publish:live:<PUBLISH_USER>:<PUBLISH_PASS>` — the user/pass
  from your `.env`. Example: `publish:live:streamer:s3cret`
- **Bonding**: enable both links (e.g. cellular + wifi). In Moblin:
  Settings → Streams → your stream → SRT(LA) → enable bonding/second connection.
- **Latency**: 2000 ms is a good starting point for cellular.
- **Bitrate**: start at 3500–5000 kbps adaptive.

## Fallbacks (no SRTLA app handy)

- **Plain SRT** (single link):
  `srt://<server-ip>:8890?streamid=publish:live:<PUBLISH_USER>:<PUBLISH_PASS>`
  Works from Larix Broadcaster, Moblin, OBS on a laptop, ffmpeg, etc.
- **RTMP** (last resort, least resilient):
  URL `rtmp://<server-ip>:1935/live?user=<PUBLISH_USER>&pass=<PUBLISH_PASS>`,
  stream key can be left empty (auth travels in the URL query).

## How the failsafe behaves

- Your phone feed drops → within ~1.5 s the server switches viewers to your BRB
  screen. The Twitch stream itself never stops.
- Feed comes back → after ~3 s of stable data the server snaps back to LIVE.
- If your connection is bouncing rapidly, the watchdog parks on BRB and waits
  for a longer stable window before returning, so viewers don't see strobing.
