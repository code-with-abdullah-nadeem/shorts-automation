"""State + video storage for GitHub Actions runs (each run starts with an empty disk).

Only active when CLOUD_STATE=1 (set by the workflows). On the PC every function is a no-op.
  pull_state()      start of a run: queue, history, schedule, YouTube sign-in
  push_state()      end of a run: whatever changed, plus videos still waiting to upload
  ensure_media(q)   download a Short's video right before it's uploaded or fixed
"""
import os, subprocess, sys
from pathlib import Path

from common import AUTO, DATA, DATA_AUTO, QUEUE, STATE, YT_DIR, load, save, log

ON = os.environ.get("CLOUD_STATE") == "1"
FILES = ["auto/queue.json", "auto/state.json", "auto/schedule.json", "youtube/token_agent.json"]
KEEP_AFTER_PUBLIC_DAYS = 3


def _node(*args):
    r = subprocess.run(["node", str(AUTO / "blobsync.mjs"), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"blob {args[0]} failed: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def pull_state():
    if not ON:
        return
    for rel in FILES:
        _node("pull", rel, str(DATA / rel))
    log("cloud: state pulled")


def ensure_media(q):
    """Make sure the Short's video is on this runner's disk. Returns its path (or None if lost)."""
    f = DATA / q["file"]
    if f.exists() or not ON:
        return f if f.exists() else None
    _node("pull", "media/" + q["file"], str(f))
    return f if f.exists() else None


def push_state():
    if not ON:
        return
    from datetime import datetime, timedelta
    queue = load(QUEUE, [])
    now = datetime.now().astimezone()
    changed = False
    for q in queue:
        f = DATA / q["file"]
        public_since = (q.get("yt") or {}).get("publish_at") or q.get("publish_at")
        old_public = ((q.get("yt") or {}).get("privacy") == "public" and public_since and
                      datetime.fromisoformat(public_since.replace("Z", "+00:00")) < now - timedelta(days=KEEP_AFTER_PUBLIC_DAYS))
        skipped_at = q.get("skipped_at")
        old_skip = q["status"] == "skipped" and (not skipped_at or  # keep skipped videos 7 days so "Restore" works
                   datetime.fromisoformat(skipped_at) < now - timedelta(days=7))
        done = q["status"] in ("deleted", "replaced") or old_skip or old_public
        if done and q.get("stored"):
            _node("del", "media/" + q["file"])  # frees space in the 1 GB free store
            q["stored"] = False
            changed = True
        elif not done and not q.get("stored") and f.exists():
            _node("push", str(f), "media/" + q["file"])
            q["stored"] = True
            changed = True
    if changed:
        save(QUEUE, queue)
    for rel in FILES:
        _node("push", str(DATA / rel), rel)  # skipped automatically when the file didn't change
    log("cloud: state pushed")


if __name__ == "__main__":  # python cloudstate.py pull|push  (used by the workflows)
    {"pull": pull_state, "push": push_state}[sys.argv[1]]()
