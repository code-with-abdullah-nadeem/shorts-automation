"""Shared paths, config and the Shorts queue (auto/queue.json).

Queue item fields:
  id, stream_id, stream_title, channel, file, title, hook, tags, description, duration,
  created, status  (ready | uploading | uploaded | failed | skipped),
  error, attempts, yt_id, publish_at, yt {privacy, upload_status, failure, rejection, views, likes, checked}
"""
import json, os, sys
from datetime import datetime
from pathlib import Path

AUTO = Path(__file__).resolve().parent   # code
ROOT = AUTO.parent
# Data lives next to the code on the PC; on the cloud worker SHORTS_DATA points at the persistent volume.
# Queue "file" paths are relative to ROOT (= DATA), e.g. "auto/output/<id>.mp4".
ROOT = DATA = Path(os.environ.get("SHORTS_DATA") or ROOT)
DATA_AUTO = DATA / "auto"
DATA_AUTO.mkdir(parents=True, exist_ok=True)
WORK = DATA_AUTO / "work"
OUTPUT = DATA_AUTO / "output"
QUEUE = DATA_AUTO / "queue.json"
STATE = DATA_AUTO / "state.json"     # processed streams + run history
SECRETS = DATA_AUTO / "secrets.json" # {"agent_token": "..."}  (never commit/share)
YT_DIR = DATA / "youtube"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def config():
    c = load(AUTO / "config.json", {})
    if os.environ.get("DASHBOARD_URL"):
        c["dashboard_url"] = os.environ["DASHBOARD_URL"]
    return c


def secrets():
    s = load(SECRETS, {})
    if os.environ.get("AGENT_TOKEN"):
        s["agent_token"] = os.environ["AGENT_TOKEN"]
    return s


LOCK = DATA_AUTO / "clip.lock"  # present while auto_clip.py runs: {pid, started, trigger, url, step}


def pid_alive(pid):
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError):
            return False
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return code.value == 259  # STILL_ACTIVE


def running():
    """Info about a clipping run in progress, or None."""
    info = load(LOCK, None)
    if info and pid_alive(info.get("pid", 0)):
        return info
    return None


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(DATA_AUTO / "pipeline.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
