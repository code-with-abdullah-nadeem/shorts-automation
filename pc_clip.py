"""Clipping on the PC (YouTube accepts downloads from the home connection, not from GitHub's servers).

Run hourly by Windows Task Scheduler (auto/pc_clip.ps1). GitHub Actions keeps doing uploads, YouTube
status checks and the dashboard; this only finds, downloads, clips and renders, then merges the new
Shorts into the shared queue in Vercel Blob.

  python auto/pc_clip.py            dashboard requests first, otherwise look for new streams
"""
import os, sys, threading
from pathlib import Path

os.environ.setdefault("CLOUD_STATE", "1")
os.environ.setdefault("SHORTS_DATA", str(Path(__file__).resolve().parent.parent / "cloud_data"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cloudstate  # noqa: E402
from common import STATE, load, log, now, running  # noqa: E402


def status(**kw):
    try:
        cloudstate.put_json(cloudstate.STATUS, {"updated": now(), **kw})
    except Exception as e:
        log(f"pc: could not report status: {e}")


def main():
    if running():
        log("pc: a clip run is already going")
        return
    cloudstate.pull_state()
    import auto_clip  # after pull: it reads config + cookies at import
    run_start = now()
    initial = {k: dict(v) for k, v in load(STATE, {"streams": {}}).get("streams", {}).items()}
    reqs = load(Path(os.environ["SHORTS_DATA"]) / cloudstate.REQUESTS, [])
    jobs = [r.get("url") for r in reqs] or [None]
    status(running=True, started=run_start, trigger="dashboard" if reqs else "schedule", url=jobs[0], step="starting")

    # report the current step to the dashboard every few minutes while clipping
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(240):
            lock = load(auto_clip.LOCK, {})
            status(running=True, started=run_start, trigger="dashboard" if reqs else "schedule",
                   url=lock.get("url"), step=lock.get("step", "clipping"))
    threading.Thread(target=heartbeat, daemon=True).start()

    done = []
    try:
        for url in dict.fromkeys(jobs):  # de-duplicated, in order
            sys.argv = ["auto_clip.py", "--trigger", "dashboard" if reqs else "schedule"] + (["--url", url] if url else [])
            auto_clip.main()
            done += [r["id"] for r in reqs if r.get("url") == url]
    finally:
        stop.set()
        cloudstate.push_clip_results(run_start, initial, done)
        status(running=False, finished=now())


if __name__ == "__main__":
    main()
