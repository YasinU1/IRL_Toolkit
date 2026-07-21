# Server setup — Oracle Cloud Always Free (Ampere A1)

## 1. Create the instance

1. Oracle Cloud console → Compute → Instances → **Create instance**.
2. Image: **Ubuntu 24.04** (aarch64). Shape: **Ampere → VM.Standard.A1.Flex**,
   **4 OCPUs / 24 GB RAM** (the full Always Free allowance — claim it all; A1
   capacity in free tier is scarce, retry different availability domains if
   "out of capacity").
3. Add your SSH public key. Note the **public IP** after creation.

## 2. Open the firewall — BOTH layers

Oracle has two firewalls and both will silently drop your ingest if you forget one.

### 2a. VCN Security List (cloud side)

Networking → your VCN → Security Lists → Default Security List → **Add Ingress Rules**:

| Source | Protocol | Dest. port | Purpose |
|---|---|---|---|
| 0.0.0.0/0 | UDP | 5000 | SRTLA bonded ingest (primary) |
| 0.0.0.0/0 | UDP | 8890 | direct SRT ingest (fallback) |
| 0.0.0.0/0 | TCP | 1935 | RTMP ingest (fallback) |
| 0.0.0.0/0 | TCP | 80  | HTTP → HTTPS redirect |
| 0.0.0.0/0 | TCP | 443 | dashboard + noVNC |
| 0.0.0.0/0 | UDP | 443 | HTTP/3 (optional) |

### 2b. iptables on the instance (the classic Oracle gotcha)

Oracle's Ubuntu images ship a restrictive iptables ruleset that **rejects
everything except SSH even after you fix the Security List**. On the server:

```bash
sudo iptables -I INPUT 6 -p udp --dport 5000 -j ACCEPT
sudo iptables -I INPUT 6 -p udp --dport 8890 -j ACCEPT
sudo iptables -I INPUT 6 -p tcp --dport 1935 -j ACCEPT
sudo iptables -I INPUT 6 -p tcp --dport 80   -j ACCEPT
sudo iptables -I INPUT 6 -p tcp --dport 443  -j ACCEPT
sudo iptables -I INPUT 6 -p udp --dport 443  -j ACCEPT
sudo netfilter-persistent save
```

## 3. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# log out and back in
```

## 4. Deploy

```bash
git clone <your repo url> irl-toolkit && cd irl-toolkit
cp .env.example .env
nano .env          # fill in every value (see below)
docker compose build   # OBS compiles from source: 20–40 min first time
docker compose up -d
docker compose logs -f watchdog   # watch it bootstrap the OBS scenes
```

`.env` notes:
- `OBS_WS_PASSWORD`: `openssl rand -hex 16`
- `DASH_PASS_HASH`: `docker compose run --rm caddy caddy hash-password`
  (paste the bcrypt output)

## 5. First-run checklist

1. `https://<public-ip>/` → log in (browser cert warning is expected unless you
   set a DOMAIN — it's Caddy's self-signed internal CA, still encrypted).
2. Paste your Twitch stream key (Twitch → Creator Dashboard → Settings → Stream).
3. Upload a BRB video or image.
4. Configure your phone app (`docs/phone-setup.md`) and start it — the
   **Phone ingest** pill should go green.
5. **Start stream** → check your Twitch dashboard.
6. Test the failsafe: toggle airplane mode on the phone. Twitch should show
   your BRB within ~2 s and return to live a few seconds after you reconnect —
   with the stream and VOD unbroken.

## Optional: real TLS with a domain

Point an A record at the public IP, set `DOMAIN=` in `.env`, and edit
`caddy/Caddyfile` per the comment at the top (replace `:443` with your domain,
remove `tls internal`). Restart caddy: `docker compose up -d caddy`.

## Sizing / performance

- x264 `veryfast` 720p30 @ 4.5 Mbps uses well under half of 4 Ampere OCPUs.
  Check with `htop` while live; raise to 1080p30 or 720p60 in
  `obs/config/profile-basic.ini` (or the OBS UI via noVNC) if there's headroom.
- Egress: a 6 Mbps output ≈ 2 TB over ~740 h/month — far inside Oracle's
  10 TB free allowance.
