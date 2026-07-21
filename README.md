# IRL-Toolkit (self-hosted)

A self-hosted replica of the core [IRL Toolkit](https://irltoolkit.com) service: a remote
OBS Studio instance running on a cloud server (Oracle Cloud Always Free, Ampere ARM64)
that ingests video from your phone over SRTLA-bonded / SRT / RTMP connections, restreams
to Twitch, and **never drops your stream** — when your phone's connection dies, a watchdog
automatically switches OBS to your BRB scene and switches back the moment you reconnect.
One continuous stream, one VOD.

## Architecture

```
Phone (Moblin / IRL Pro / BELABOX app — SRTLA over wifi + cellular)
      │ UDP :5000 (bonded SRTLA links)
      ▼
  srtla_rec ── de-bonds N links into 1 SRT stream
      │
      ▼
  MediaMTX ◄── also direct SRT (UDP :8890) and RTMP (TCP :1935) fallback ingest
      │  one internal URL: srt://mediamtx:8890?streamid=read:live
      ▼
  OBS Studio (headless ARM64 build, Xvfb + noVNC, obs-websocket)
      │  LIVE scene ⇄ BRB scene (switched by watchdog)
      ▼
  Twitch (rtmp://live.twitch.tv/app/<your key>)

  watchdog  — polls MediaMTX for ingest health, drives OBS scene failover
  dashboard — web UI: stream key, BRB media upload, status, start/stop
  caddy     — TLS + basic auth in front of dashboard and noVNC
```

## Quick start

1. Copy `.env.example` to `.env` and fill in every value.
2. `docker compose build` (the OBS image compiles OBS Studio from source — expect
   20–40 minutes on first build).
3. `docker compose up -d`
4. Open `https://<server-ip>/` — set your Twitch stream key, upload a BRB video/image.
5. Point your phone app at `srtla://<server-ip>:5000` (see `docs/phone-setup.md`).
6. Hit **Start Stream** in the dashboard.

Full server setup (Oracle Cloud instance, firewall rules, the Oracle iptables gotcha):
see `docs/setup.md`.

## Layout

| Path | Purpose |
|---|---|
| `docker-compose.yml` | All services |
| `obs/` | OBS Studio ARM64 source build + headless entrypoint |
| `srtla/` | BELABOX `srtla_rec` bonding receiver build |
| `mediamtx/` | Ingest server config |
| `watchdog/` | BRB failover daemon (the "never drop" logic) |
| `dashboard/` | FastAPI control panel |
| `caddy/` | Reverse proxy / TLS / auth |
| `docs/` | Server + phone setup guides |
