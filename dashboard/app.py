"""Control-panel backend: stream key, BRB media, status, start/stop.

Talks to OBS over obs-websocket and reads ingest health from the watchdog.
Persists config in /data/config.json (volume) — the Twitch key never lives
in the repo or the image.
"""

import json
import os
import secrets
import shutil
import time
from pathlib import Path

import bcrypt
import requests
import obsws_python as obsws
from fastapi import Cookie, FastAPI, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

OBS_HOST = os.environ.get("OBS_HOST", "obs")
OBS_PORT = int(os.environ.get("OBS_PORT", "4455"))
OBS_PASSWORD = os.environ["OBS_WS_PASSWORD"]
WATCHDOG_URL = os.environ.get("WATCHDOG_URL", "http://watchdog:8081")

CONFIG_FILE = Path("/data/config.json")
MEDIA_DIR = Path("/media/brb")  # same path inside the OBS container
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".ts", ".flv"}

SCENE_BRB = "BRB"
SOURCE_BRB_MEDIA = "BRB Media"
SOURCE_BRB_IMAGE = "BRB Image"

DASH_USER = os.environ.get("DASH_USER", "admin")
DASH_PASS_HASH = os.environ.get("DASH_PASS_HASH", "")

SESSION_TTL = 7 * 86400
_sessions: dict[str, float] = {}  # token -> expiry (in-memory; re-login on restart)

app = FastAPI(title="IRL-Toolkit dashboard")


def session_valid(token):
    exp = _sessions.get(token)
    if not exp or exp < time.time():
        _sessions.pop(token, None)
        return False
    return True


class Login(BaseModel):
    username: str
    password: str


@app.get("/login")
def login_page():
    return FileResponse("static/login.html")


@app.post("/api/login")
def login(body: Login, response: Response):
    ok = secrets.compare_digest(body.username, DASH_USER) and bcrypt.checkpw(
        body.password.encode(), DASH_PASS_HASH.encode()
    )
    if not ok:
        time.sleep(0.5)  # blunt brute-force damper
        raise HTTPException(status_code=401, detail="wrong username or password")
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    response.set_cookie(
        "session", token, max_age=SESSION_TTL,
        httponly=True, secure=True, samesite="lax",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response, session: str | None = Cookie(default=None)):
    if session:
        _sessions.pop(session, None)
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/auth/verify")
def auth_verify(session: str | None = Cookie(default=None)):
    """Caddy forward_auth hits this for every protected request."""
    if session and session_valid(session):
        return Response(status_code=204)
    return RedirectResponse("/login", status_code=302)


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def obs_client():
    try:
        return obsws.ReqClient(
            host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"OBS unreachable: {e}")


class StreamKey(BaseModel):
    key: str


@app.get("/api/status")
def status():
    out = {"watchdog": None, "obs": None, "config": {}}
    try:
        out["watchdog"] = requests.get(WATCHDOG_URL, timeout=2).json()
    except requests.RequestException:
        pass
    try:
        cl = obs_client()
        s = cl.get_stream_status()
        out["obs"] = {
            "streaming": s.output_active,
            "duration_ms": s.output_duration,
            "dropped_frames": s.output_skipped_frames,
            "total_frames": s.output_total_frames,
        }
    except HTTPException:
        pass
    cfg = load_config()
    out["config"] = {
        "stream_key_set": bool(cfg.get("stream_key")),
        "brb_file": cfg.get("brb_file"),
    }
    return out


@app.post("/api/stream-key")
def set_stream_key(body: StreamKey):
    key = body.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="empty key")
    cl = obs_client()
    cl.set_stream_service_settings(
        "rtmp_custom",
        {"server": "rtmp://live.twitch.tv/app", "key": key, "use_auth": False},
    )
    cfg = load_config()
    cfg["stream_key"] = key
    save_config(cfg)
    return {"ok": True}


@app.post("/api/brb")
async def upload_brb(file: UploadFile):
    ext = Path(file.filename or "brb").suffix.lower()
    if ext not in IMAGE_EXTS | VIDEO_EXTS:
        raise HTTPException(status_code=400, detail=f"unsupported type {ext}")
    dest = MEDIA_DIR / f"brb{ext}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    cl = obs_client()
    is_image = ext in IMAGE_EXTS
    if is_image:
        cl.set_input_settings(SOURCE_BRB_IMAGE, {"file": str(dest)}, True)
    else:
        cl.set_input_settings(
            SOURCE_BRB_MEDIA,
            {"is_local_file": True, "local_file": str(dest), "looping": True},
            True,
        )
    # Enable the matching source in the BRB scene, disable the other.
    for name, enabled in (
        (SOURCE_BRB_IMAGE, is_image),
        (SOURCE_BRB_MEDIA, not is_image),
    ):
        item_id = cl.get_scene_item_id(SCENE_BRB, name).scene_item_id
        cl.set_scene_item_enabled(SCENE_BRB, item_id, enabled)

    cfg = load_config()
    cfg["brb_file"] = dest.name
    save_config(cfg)
    return {"ok": True, "file": dest.name}


@app.post("/api/stream/start")
def start_stream():
    cfg = load_config()
    if not cfg.get("stream_key"):
        raise HTTPException(status_code=400, detail="set your stream key first")
    cl = obs_client()
    # Re-assert the key each start in case the OBS container was recreated.
    cl.set_stream_service_settings(
        "rtmp_custom",
        {
            "server": "rtmp://live.twitch.tv/app",
            "key": cfg["stream_key"],
            "use_auth": False,
        },
    )
    cl.start_stream()
    return {"ok": True}


@app.post("/api/stream/stop")
def stop_stream():
    cl = obs_client()
    cl.stop_stream()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
