"""BRB failover watchdog — the "never drop again" logic.

Polls MediaMTX's API to decide whether the phone's ingest is genuinely alive
(publisher connected AND bytes still flowing — a stalled-but-connected input
counts as dead), and drives OBS scene switching over obs-websocket:

    LIVE --(ingest down >= DOWN_THRESHOLD_SEC)--> BRB
    BRB  --(ingest healthy >= UP_THRESHOLD_SEC)--> restart media source -> LIVE

Also bootstraps the LIVE/BRB scenes and sources in OBS on startup (idempotent),
so a fresh OBS container self-configures instead of relying on hand-written
scene-collection JSON. Exposes its state as JSON on :8081 for the dashboard.
"""

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import obsws_python as obsws

log = logging.getLogger("watchdog")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OBS_HOST = os.environ.get("OBS_HOST", "obs")
OBS_PORT = int(os.environ.get("OBS_PORT", "4455"))
OBS_PASSWORD = os.environ["OBS_WS_PASSWORD"]
MEDIAMTX_API = os.environ.get("MEDIAMTX_API", "http://mediamtx:9997")
INGEST_PATH = os.environ.get("INGEST_PATH", "live")
SRT_READ_URL = os.environ.get(
    "SRT_READ_URL", "srt://mediamtx:8890?streamid=read:live&latency=2000000"
)
TICK_SEC = float(os.environ.get("TICK_SEC", "0.5"))
DOWN_THRESHOLD = float(os.environ.get("DOWN_THRESHOLD_SEC", "1.5"))
UP_THRESHOLD = float(os.environ.get("UP_THRESHOLD_SEC", "3.0"))
MIN_LIVE_DWELL = float(os.environ.get("MIN_LIVE_DWELL_SEC", "5.0"))

SCENE_LIVE = "LIVE"
SCENE_BRB = "BRB"
SOURCE_INGEST = "Ingest"
SOURCE_BRB_MEDIA = "BRB Media"
SOURCE_BRB_IMAGE = "BRB Image"
CANVAS_W, CANVAS_H = 1920, 1080

# Shared with the HTTP status server.
state = {
    "scene": "unknown",
    "ingest_healthy": False,
    "ingest_bitrate_kbps": 0,
    "obs_connected": False,
    "last_transition": None,
    "transitions": 0,
}
state_lock = threading.Lock()


def set_state(**kwargs):
    with state_lock:
        state.update(kwargs)


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with state_lock:
            body = json.dumps(state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class IngestMonitor:
    """Health = publisher present AND bytesReceived advancing between ticks."""

    def __init__(self):
        self.last_bytes = None
        self.last_check = None

    def healthy(self):
        try:
            r = requests.get(
                f"{MEDIAMTX_API}/v3/paths/get/{INGEST_PATH}", timeout=2
            )
            if r.status_code != 200:
                self.last_bytes = None
                return False
            info = r.json()
            if not info.get("ready"):
                self.last_bytes = None
                return False
            now = time.monotonic()
            bytes_rx = info.get("bytesReceived", 0)
            progressed = self.last_bytes is None or bytes_rx != self.last_bytes
            if self.last_bytes is not None and bytes_rx > self.last_bytes and self.last_check:
                elapsed = now - self.last_check
                if elapsed > 0:
                    kbps = int((bytes_rx - self.last_bytes) * 8 / 1000 / elapsed)
                    set_state(ingest_bitrate_kbps=kbps)
            self.last_bytes = bytes_rx
            self.last_check = now
            return progressed
        except requests.RequestException:
            self.last_bytes = None
            return False


def connect_obs():
    while True:
        try:
            cl = obsws.ReqClient(
                host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5
            )
            set_state(obs_connected=True)
            log.info("connected to obs-websocket at %s:%s", OBS_HOST, OBS_PORT)
            return cl
        except Exception as e:
            set_state(obs_connected=False)
            log.warning("obs not reachable yet (%s), retrying in 3s", e)
            time.sleep(3)


def ensure_scenes(cl):
    """Idempotently create LIVE/BRB scenes and their sources."""
    existing = {s["sceneName"] for s in cl.get_scene_list().scenes}
    for scene in (SCENE_LIVE, SCENE_BRB):
        if scene not in existing:
            cl.create_scene(scene)
            log.info("created scene %s", scene)

    def input_names():
        return {i["inputName"] for i in cl.get_input_list().inputs}

    def stretch_to_canvas(scene, source):
        item_id = cl.get_scene_item_id(scene, source).scene_item_id
        cl.set_scene_item_transform(
            scene,
            item_id,
            {
                "boundsType": "OBS_BOUNDS_SCALE_INNER",
                "boundsAlignment": 0,
                "boundsWidth": CANVAS_W,
                "boundsHeight": CANVAS_H,
                "positionX": 0,
                "positionY": 0,
            },
        )

    if SOURCE_INGEST not in input_names():
        cl.create_input(
            SCENE_LIVE,
            SOURCE_INGEST,
            "ffmpeg_source",
            {
                "input": SRT_READ_URL,
                "is_local_file": False,
                "buffering_mb": 2,
                "reconnect_delay_sec": 1,
                "restart_on_activate": False,
                "clear_on_media_end": False,
                "hw_decode": False,
            },
            True,
        )
        stretch_to_canvas(SCENE_LIVE, SOURCE_INGEST)
        log.info("created ingest media source -> %s", SRT_READ_URL)

    if SOURCE_BRB_MEDIA not in input_names():
        cl.create_input(
            SCENE_BRB,
            SOURCE_BRB_MEDIA,
            "ffmpeg_source",
            {"is_local_file": True, "local_file": "", "looping": True},
            True,
        )
        stretch_to_canvas(SCENE_BRB, SOURCE_BRB_MEDIA)

    if SOURCE_BRB_IMAGE not in input_names():
        cl.create_input(
            SCENE_BRB, SOURCE_BRB_IMAGE, "image_source", {"file": ""}, False
        )
        stretch_to_canvas(SCENE_BRB, SOURCE_BRB_IMAGE)

    current = cl.get_current_program_scene().current_program_scene_name
    if current not in (SCENE_LIVE, SCENE_BRB):
        cl.set_current_program_scene(SCENE_LIVE)
        current = SCENE_LIVE
    set_state(scene=current)
    return current


def run():
    threading.Thread(
        target=ThreadingHTTPServer(("0.0.0.0", 8081), StatusHandler).serve_forever,
        daemon=True,
    ).start()

    monitor = IngestMonitor()
    cl = connect_obs()
    scene = ensure_scenes(cl)

    down_since = None
    up_since = None
    live_entered_at = time.monotonic()
    # Anti-flap: if the last stint in LIVE was shorter than MIN_LIVE_DWELL,
    # the connection is bouncing — demand a longer stable period before the
    # next return, so viewers see a steady BRB instead of strobing scenes.
    required_up = UP_THRESHOLD

    while True:
        time.sleep(TICK_SEC)
        healthy = monitor.healthy()
        if not healthy:
            set_state(ingest_bitrate_kbps=0)
        set_state(ingest_healthy=healthy)
        now = time.monotonic()

        try:
            if scene == SCENE_LIVE:
                up_since = None
                if healthy:
                    down_since = None
                else:
                    down_since = down_since or now
                    if now - down_since >= DOWN_THRESHOLD:
                        log.warning("ingest down %.1fs -> switching to BRB",
                                    now - down_since)
                        flapping = now - live_entered_at < MIN_LIVE_DWELL
                        required_up = UP_THRESHOLD * (3 if flapping else 1)
                        cl.set_current_program_scene(SCENE_BRB)
                        scene = SCENE_BRB
                        set_state(scene=scene, transitions=state["transitions"] + 1,
                                  last_transition=time.strftime("%Y-%m-%dT%H:%M:%S"))
            else:  # BRB
                down_since = None
                if not healthy:
                    up_since = None
                else:
                    up_since = up_since or now
                    if now - up_since >= required_up:
                        log.info("ingest healthy %.1fs -> restarting source, back to LIVE",
                                 now - up_since)
                        # Re-latch onto the fresh SRT publisher session.
                        cl.trigger_media_input_action(
                            SOURCE_INGEST,
                            "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
                        )
                        time.sleep(1.0)
                        cl.set_current_program_scene(SCENE_LIVE)
                        scene = SCENE_LIVE
                        live_entered_at = time.monotonic()
                        up_since = None
                        set_state(scene=scene, transitions=state["transitions"] + 1,
                                  last_transition=time.strftime("%Y-%m-%dT%H:%M:%S"))
        except Exception as e:
            log.error("obs call failed (%s) — reconnecting", e)
            set_state(obs_connected=False)
            cl = connect_obs()
            scene = ensure_scenes(cl)


if __name__ == "__main__":
    run()
