# Project Plan — Self-Hosted IRL Toolkit Replica

## Goal

Replicate the core of irltoolkit.com for personal use: a remote OBS instance on a cloud
server that ingests video from a phone over bonded cellular/wifi, restreams to Twitch
with my stream key, and — the signature feature — **never drops the stream**. When the
phone's ingest connection dies, the server automatically shows a customized "Be Right
Back" scene to viewers and keeps the Twitch output running, then switches back to the
live feed the moment the phone reconnects. One continuous stream, one unbroken VOD.

## Locked-in decisions

- **Full remote OBS** — real OBS Studio running headless in Docker (Xvfb virtual display,
  noVNC for browser access, obs-websocket for programmatic control). Not a lightweight
  FFmpeg relay; we get real scenes, overlays, and production features.
- **Ingest**: SRTLA (BELABOX's bonding protocol — combines wifi + cellular into one
  resilient link) as primary, plus plain SRT and RTMP as fallback paths.
- **Hosting**: Oracle Cloud Always Free tier, Ampere A1 shape (4 OCPU / 24 GB RAM,
  real public IP, 10 TB/month egress). This is **ARM64**, so OBS is built from source —
  no official ARM64 Linux build exists.
- **Control**: custom web dashboard (stream key, BRB media upload, connection health,
  start/stop) rather than only remoting into OBS.

## Service topology

```
Phone app (Moblin / IRL Pro / BELABOX encoder — SRTLA over 2 bonded links)
        │  UDP :5000
        ▼
  srtla_rec        de-bonds the links back into one standard SRT stream
        │
        ▼
  MediaMTX         normalizes SRTLA→SRT, direct SRT (:8890/udp) and RTMP (:1935)
        │          into one internal URL; its HTTP API reports ingest health
        ▼
  OBS (headless)   LIVE scene = Media Source reading MediaMTX's SRT URL
        │          BRB scene = my uploaded video/image
        ▼          encodes x264 and pushes to Twitch continuously
  Twitch

  watchdog   — polls MediaMTX ("is the publisher connected and are bytes flowing?"),
               flips OBS between LIVE and BRB over obs-websocket
  dashboard  — FastAPI + single static page
  caddy      — TLS + basic auth in front of dashboard and noVNC
```

**Why MediaMTX in the middle:** it gives OBS one stable URL to read regardless of which
protocol the phone used, and its API gives the watchdog a clean liveness signal
(publisher present + bytes advancing) — far more reliable than inferring health from
inside OBS. A connected-but-stalled input counts as *down*.

**Why the watchdog is an external process** (not an OBS Lua script): it must query
MediaMTX as well as OBS, and it should survive/log independently of OBS itself.

## The BRB failover state machine

- Tick every 500 ms; ingest is **healthy** iff the publisher is connected AND
  `bytesReceived` increased since the last tick.
- `LIVE` → down for ≥ 1.5 s → switch program scene to `BRB`.
- `BRB` → healthy for ≥ 3 s continuously → restart the Media Source (so it re-latches
  onto the fresh publisher session) → switch back to `LIVE`.
- Anti-flap: after returning to LIVE, a 5 s dwell before another transition is allowed,
  so a bouncing connection parks on BRB instead of strobing viewers.

## Build phases

### Phase 1 — Base plumbing *(validates the riskiest unknowns first)*
MediaMTX + the OBS ARM64 container, RTMP-only ingest, manual restream to Twitch.
Proves: the OBS source build works on ARM64, headless OBS is stable under Xvfb without
a GPU, and 4 Ampere OCPUs handle x264 at the target bitrate.
**Done when:** phone pushes RTMP → server → visible live on Twitch, CPU < ~60 %.

### Phase 2 — The "never drop" watchdog *(the core value prop)*
Add the failover daemon and the LIVE/BRB scene bootstrap.
**Done when:** toggling airplane mode mid-stream shows BRB on Twitch within ~2 s,
returns to live within ~5 s of reconnect, and the VOD stays unbroken.

### Phase 3 — Bonded ingest (SRT + SRTLA)
Build `srtla_rec`, open the SRT/SRTLA ports, configure the phone app with 2 links.
**Done when:** killing wifi mid-stream keeps the feed alive over cellular alone,
without the BRB even triggering.

### Phase 4 — Dashboard + TLS
FastAPI control panel (stream key, BRB upload, status, start/stop), Caddy with
basic auth, setup docs.
**Done when:** a full streaming session can be driven entirely from a phone browser.

## Known risks

| Risk | Mitigation |
|---|---|
| OBS ARM64 source build needs iteration | Done first (Phase 1); dev on Apple Silicon matches the target arch |
| No browser (CEF) sources on ARM initially | Skipped deliberately (`-DENABLE_BROWSER=OFF`); revisit later if overlays need it |
| `srtla_rec` has no official packages | Small C program, builds clean on ARM64 (BELABOX community standard) |
| No GPU on Ampere A1 | Software GL (llvmpipe) for OBS canvas + x264 CPU encode — known-working headless pattern |
