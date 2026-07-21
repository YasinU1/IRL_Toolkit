"""Control-panel backend: stream key, BRB media, multi-ingest, scenes, status.

Talks to OBS over obs-websocket and to MediaMTX over its HTTP API.
Persists config in /data/config.json (volume) — the Twitch key never lives
in the repo or the image.

Ingest model (mirrors IRL Toolkit): each ingest is a MediaMTX path plus an
OBS scene containing one media source that reads that path. The built-in
"Main Ingest" is the watchdog-managed LIVE scene; extra ingests are created
at runtime (path added via MediaMTX config API, scene via obs-websocket) and
switched to manually from the Scene card.
"""

import json
import os
import re
import secrets
import shutil
import threading
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
MEDIAMTX_API = os.environ.get("MEDIAMTX_API", "http://mediamtx:9997")
SRT_READ_TEMPLATE = os.environ.get(
    "SRT_READ_TEMPLATE", "srt://mediamtx:8890?streamid=read:{path}&latency=2000000"
)

CONFIG_FILE = Path("/data/config.json")
MEDIA_DIR = Path("/media/brb")  # same path inside the OBS container
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".ts", ".flv"}

SCENE_BRB = "BRB"
SOURCE_BRB_MEDIA = "BRB Media"
SOURCE_BRB_IMAGE = "BRB Image"
MAIN_INGEST = {"name": "Main Ingest", "path": "live",
               "scene": "LIVE", "source": "Ingest", "builtin": True}
CANVAS_W, CANVAS_H = 1920, 1080

DASH_USER = os.environ.get("DASH_USER", "admin")
DASH_PASS_HASH = os.environ.get("DASH_PASS_HASH", "")

SESSION_TTL = 7 * 86400
_sessions: dict[str, float] = {}  # token -> expiry (in-memory; re-login on restart)

app = FastAPI(title="IRL-Toolkit dashboard")


# ---------- config ----------

_cfg_lock = threading.Lock()


def load_config():
    cfg = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
    cfg.setdefault("ingests", [dict(MAIN_INGEST)])
    return cfg


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ---------- OBS client (persistent, reconnect on failure) ----------

_obs_lock = threading.Lock()
_obs_cl = None


def obs_client():
    global _obs_cl
    with _obs_lock:
        if _obs_cl is None:
            try:
                _obs_cl = obsws.ReqClient(
                    host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5
                )
            except Exception as e:
                raise HTTPException(status_code=503, detail=f"OBS unreachable: {e}")
        return _obs_cl


def obs_reset():
    global _obs_cl
    with _obs_lock:
        try:
            if _obs_cl:
                _obs_cl.disconnect()
        except Exception:
            pass
        _obs_cl = None


def with_obs(fn):
    """Run fn(client); on connection failure reconnect once and retry."""
    try:
        return fn(obs_client())
    except HTTPException:
        raise
    except Exception:
        obs_reset()
        try:
            return fn(obs_client())
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"OBS call failed: {e}")


# ---------- MediaMTX helpers ----------

def mtx_paths():
    try:
        r = requests.get(f"{MEDIAMTX_API}/v3/paths/list", timeout=2)
        return {p["name"]: p for p in r.json().get("items", [])}
    except requests.RequestException:
        return {}


def mtx_add_path(path):
    r = requests.post(
        f"{MEDIAMTX_API}/v3/config/paths/add/{path}",
        json={"source": "publisher", "overridePublisher": True},
        timeout=3,
    )
    if r.status_code not in (200, 400):  # 400 = already exists
        raise HTTPException(status_code=502,
                            detail=f"MediaMTX rejected path: {r.text[:200]}")


def mtx_delete_path(path):
    requests.post(f"{MEDIAMTX_API}/v3/config/paths/delete/{path}", timeout=3)


def restore_extra_paths():
    """MediaMTX runtime path config is lost on its restart — re-add ours."""
    for _ in range(30):
        try:
            requests.get(f"{MEDIAMTX_API}/v3/paths/list", timeout=2)
            break
        except requests.RequestException:
            time.sleep(2)
    for ing in load_config()["ingests"]:
        if not ing.get("builtin"):
            try:
                mtx_add_path(ing["path"])
            except Exception:
                pass


threading.Thread(target=restore_extra_paths, daemon=True).start()


# ---------- auth ----------

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


# ---------- status ----------

@app.get("/api/status")
def status():
    out = {"watchdog": None, "obs": None, "scenes": None, "ingests": [],
           "config": {}}
    try:
        out["watchdog"] = requests.get(WATCHDOG_URL, timeout=2).json()
    except requests.RequestException:
        pass

    def obs_part(cl):
        s = cl.get_stream_status()
        sc = cl.get_scene_list()
        return (
            {
                "streaming": s.output_active,
                "duration_ms": s.output_duration,
                "dropped_frames": s.output_skipped_frames,
                "total_frames": s.output_total_frames,
            },
            {
                "current": sc.current_program_scene_name,
                "all": [x["sceneName"] for x in reversed(sc.scenes)],
            },
        )

    muted = {}
    try:
        out["obs"], out["scenes"] = with_obs(obs_part)
        for ing in load_config()["ingests"]:
            try:
                muted[ing["source"]] = with_obs(
                    lambda cl, s=ing["source"]: cl.get_input_mute(s).input_muted
                )
            except HTTPException:
                pass
    except HTTPException:
        pass

    paths = mtx_paths()
    for ing in load_config()["ingests"]:
        p = paths.get(ing["path"], {})
        out["ingests"].append({
            "name": ing["name"],
            "path": ing["path"],
            "scene": ing["scene"],
            "builtin": bool(ing.get("builtin")),
            "online": bool(p.get("ready")),
            "bytes_received": p.get("bytesReceived", 0),
            "muted": muted.get(ing["source"]),
        })

    cfg = load_config()
    out["config"] = {
        "stream_key_set": bool(cfg.get("stream_key")),
        "brb_file": cfg.get("brb_file"),
    }
    return out


# ---------- stream control ----------

class StreamKey(BaseModel):
    key: str


def push_stream_key(cl, key):
    cl.set_stream_service_settings(
        "rtmp_custom",
        {"server": "rtmp://live.twitch.tv/app", "key": key, "use_auth": False},
    )


@app.post("/api/stream-key")
def set_stream_key(body: StreamKey):
    key = body.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="empty key")
    with_obs(lambda cl: push_stream_key(cl, key))
    with _cfg_lock:
        cfg = load_config()
        cfg["stream_key"] = key
        save_config(cfg)
    return {"ok": True}


@app.post("/api/stream/start")
def start_stream():
    cfg = load_config()
    if not cfg.get("stream_key"):
        raise HTTPException(status_code=400, detail="set your stream key first")

    def go(cl):
        push_stream_key(cl, cfg["stream_key"])  # re-assert after OBS recreate
        cl.start_stream()

    with_obs(go)
    return {"ok": True}


@app.post("/api/stream/stop")
def stop_stream():
    with_obs(lambda cl: cl.stop_stream())
    return {"ok": True}


# ---------- scenes ----------

class SceneReq(BaseModel):
    name: str


@app.post("/api/scene")
def set_scene(body: SceneReq):
    with_obs(lambda cl: cl.set_current_program_scene(body.name))
    return {"ok": True}


# ---------- ingests ----------

class IngestReq(BaseModel):
    name: str


def stretch_to_canvas(cl, scene, source):
    item_id = cl.get_scene_item_id(scene, source).scene_item_id
    cl.set_scene_item_transform(scene, item_id, {
        "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsAlignment": 0,
        "boundsWidth": CANVAS_W, "boundsHeight": CANVAS_H,
        "positionX": 0, "positionY": 0,
    })


@app.post("/api/ingests")
def add_ingest(body: IngestReq):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="empty name")
    path = re.sub(r"[^a-z0-9]+", "", name.lower())
    if not path:
        raise HTTPException(status_code=400, detail="name needs letters/numbers")
    with _cfg_lock:
        cfg = load_config()
        if any(i["path"] == path or i["name"] == name for i in cfg["ingests"]):
            raise HTTPException(status_code=400, detail="ingest already exists")

        mtx_add_path(path)
        source = f"{name} Source"

        def build(cl):
            scenes = {s["sceneName"] for s in cl.get_scene_list().scenes}
            if name not in scenes:
                cl.create_scene(name)
            cl.create_input(name, source, "ffmpeg_source", {
                "input": SRT_READ_TEMPLATE.format(path=path),
                "is_local_file": False, "buffering_mb": 2,
                "reconnect_delay_sec": 1, "restart_on_activate": False,
                "clear_on_media_end": False, "hw_decode": False,
            }, True)
            stretch_to_canvas(cl, name, source)

        with_obs(build)
        cfg["ingests"].append({"name": name, "path": path, "scene": name,
                               "source": source, "builtin": False})
        save_config(cfg)
    return {"ok": True, "path": path}


@app.delete("/api/ingests/{path}")
def delete_ingest(path: str):
    with _cfg_lock:
        cfg = load_config()
        ing = next((i for i in cfg["ingests"] if i["path"] == path), None)
        if not ing:
            raise HTTPException(status_code=404, detail="no such ingest")
        if ing.get("builtin"):
            raise HTTPException(status_code=400, detail="cannot delete Main Ingest")

        def teardown(cl):
            try:
                cl.remove_input(ing["source"])
            except Exception:
                pass
            try:
                cl.remove_scene(ing["scene"])
            except Exception:
                pass

        with_obs(teardown)
        mtx_delete_path(path)
        cfg["ingests"] = [i for i in cfg["ingests"] if i["path"] != path]
        save_config(cfg)
    return {"ok": True}


@app.post("/api/ingests/{path}/fix")
def fix_ingest(path: str):
    ing = next((i for i in load_config()["ingests"] if i["path"] == path), None)
    if not ing:
        raise HTTPException(status_code=404, detail="no such ingest")
    with_obs(lambda cl: cl.trigger_media_input_action(
        ing["source"], "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"))
    return {"ok": True}


@app.post("/api/ingests/{path}/mute")
def mute_ingest(path: str):
    ing = next((i for i in load_config()["ingests"] if i["path"] == path), None)
    if not ing:
        raise HTTPException(status_code=404, detail="no such ingest")

    def toggle(cl):
        cl.toggle_input_mute(ing["source"])
        return cl.get_input_mute(ing["source"]).input_muted

    return {"ok": True, "muted": with_obs(toggle)}


# ---------- BRB media ----------

@app.post("/api/brb")
async def upload_brb(file: UploadFile):
    ext = Path(file.filename or "brb").suffix.lower()
    if ext not in IMAGE_EXTS | VIDEO_EXTS:
        raise HTTPException(status_code=400, detail=f"unsupported type {ext}")
    dest = MEDIA_DIR / f"brb{ext}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    is_image = ext in IMAGE_EXTS

    def apply(cl):
        if is_image:
            cl.set_input_settings(SOURCE_BRB_IMAGE, {"file": str(dest)}, True)
        else:
            cl.set_input_settings(SOURCE_BRB_MEDIA, {
                "is_local_file": True, "local_file": str(dest), "looping": True,
            }, True)
        for src, enabled in ((SOURCE_BRB_IMAGE, is_image),
                             (SOURCE_BRB_MEDIA, not is_image)):
            item_id = cl.get_scene_item_id(SCENE_BRB, src).scene_item_id
            cl.set_scene_item_enabled(SCENE_BRB, item_id, enabled)

    with_obs(apply)
    with _cfg_lock:
        cfg = load_config()
        cfg["brb_file"] = dest.name
        save_config(cfg)
    return {"ok": True, "file": dest.name}


@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
