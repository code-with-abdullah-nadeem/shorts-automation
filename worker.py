"""Always-on cloud worker (Railway). Replaces the two Windows scheduled tasks.

Every 5 minutes  : agent.py  (dashboard actions, uploads, YouTube status, dashboard sync)
Every N hours    : auto_clip.py in the background (find + clip new GTA 6 streams), N = config clip_every_hours
Once a day       : delete old downloads/renders so the volume doesn't fill up

Secrets come from environment variables and are written to the volume on first start:
  YT_CLIENT_SECRET_JSON  Google OAuth client (client_secret.json contents)
  YT_TOKEN_JSON          YouTube sign-in (token_agent.json contents); refreshed tokens are kept on the volume
  YT_COOKIES_B64         base64 of cookies.txt (spare account) for downloads
  AGENT_TOKEN, DASHBOARD_URL, CLAUDE_CODE_OAUTH_TOKEN, SHORTS_DATA
"""
import base64, os, shutil, subprocess, sys, time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import AUTO, DATA_AUTO, QUEUE, WORK, OUTPUT, YT_DIR, config, load, save, log, now, running

SYNC_EVERY = 5 * 60


def bootstrap_seed():
    """First start only: copy the queue/history/pending videos that were moved over from the PC."""
    seed = AUTO / "seed" / "data"
    if seed.exists() and not QUEUE.exists():
        shutil.copytree(seed, DATA_AUTO.parent, dirs_exist_ok=True)
        log("worker: imported the queue and pending Shorts from the PC")


def bootstrap_secrets():
    YT_DIR.mkdir(parents=True, exist_ok=True)
    env_files = {"YT_CLIENT_SECRET_JSON": YT_DIR / "client_secret.json"}
    for var, path in env_files.items():
        if os.environ.get(var):
            path.write_text(os.environ[var], encoding="utf-8")
    tok = YT_DIR / "token_agent.json"
    if os.environ.get("YT_TOKEN_JSON") and not tok.exists():  # keep refreshed tokens across restarts
        tok.write_text(os.environ["YT_TOKEN_JSON"], encoding="utf-8")
    if os.environ.get("YT_COOKIES_B64"):
        (YT_DIR / "cookies.txt").write_bytes(base64.b64decode(os.environ["YT_COOKIES_B64"]))


def start_clip_run():
    if running():
        return
    log("worker: starting scheduled clip run")
    logf = open(DATA_AUTO / "clip_task.log", "a", encoding="utf-8")
    subprocess.Popen([sys.executable, str(AUTO / "auto_clip.py"), "--trigger", "schedule"], cwd=AUTO,
                     stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)


def cleanup(keep_days=3):
    """Free space: raw downloads of finished streams, and renders already live on YouTube."""
    cutoff = time.time() - keep_days * 86400
    state = load(DATA_AUTO / "state.json", {"streams": {}})
    for vid, s in state.get("streams", {}).items():
        d = WORK / vid
        if s.get("status") == "done" and d.exists() and d.stat().st_mtime < cutoff:
            for f in d.rglob("*"):
                if f.suffix in (".mp4", ".m4a", ".part", ".webm"):
                    f.unlink(missing_ok=True)
            log(f"worker: cleaned downloads of {vid}")
    for q in load(QUEUE, []):
        f = DATA_AUTO.parent / q["file"]
        live = (q.get("yt") or {}).get("privacy") == "public" or q["status"] in ("deleted", "replaced", "skipped")
        if live and f.exists() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


def main():
    bootstrap_seed()
    bootstrap_secrets()
    hours = config().get("clip_every_hours", 3)
    sched = load(DATA_AUTO / "schedule.json", {})
    next_clip = datetime.fromisoformat(sched["next_clip_run"]) if sched.get("next_clip_run") else datetime.now().astimezone() + timedelta(minutes=2)
    last_cleanup = 0.0
    log(f"worker: up. clipping every {hours} h, syncing every {SYNC_EVERY // 60} min; next clip run {next_clip:%a %H:%M}")
    while True:
        t0 = time.time()
        if datetime.now().astimezone() >= next_clip:
            start_clip_run()
            next_clip = datetime.now().astimezone() + timedelta(hours=hours)
        save(DATA_AUTO / "schedule.json", {"next_clip_run": next_clip.isoformat(), "updated": now()})
        try:
            subprocess.run([sys.executable, str(AUTO / "agent.py")], cwd=AUTO, timeout=3 * 3600)
        except subprocess.TimeoutExpired:
            log("worker: agent run timed out")
        if time.time() - last_cleanup > 86400:
            try:
                cleanup()
            except Exception as e:
                log(f"worker: cleanup failed: {e}")
            last_cleanup = time.time()
        time.sleep(max(30, SYNC_EVERY - (time.time() - t0)))


if __name__ == "__main__":
    main()
