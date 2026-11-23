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
FILES = ["auto/queue.json", "auto/state.json", "auto/schedule.json", "youtube/token_agent.json",
         "auto/clip_requests.json", "auto/clip_status.json"]
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
    # Shorts the PC added to the shared queue while this run was busy: keep them (never overwrite them away)
    mine = {q["id"] for q in queue}
    extra = [q for q in remote_json("auto/queue.json", []) if q["id"] not in mine]
    if extra:
        queue += extra
        changed = True
        log(f"cloud: kept {len(extra)} shorts added by the PC meanwhile")
    if changed:
        save(QUEUE, queue)
    for rel in FILES:
        _node("push", str(DATA / rel), rel)  # skipped automatically when the file didn't change
    log("cloud: state pushed")


# ---------- PC clipping (downloads only work from the home connection) ----------
REQUESTS = "auto/clip_requests.json"   # dashboard "check now" / "clip this link" -> picked up by the PC
STATUS = "auto/clip_status.json"       # written by the PC while it clips -> shown on the dashboard


def remote_json(rel, default=None):
    """Fresh copy of a shared file, without touching the local one."""
    tmp = DATA / (rel + ".remote")
    tmp.unlink(missing_ok=True)
    _node("pull", rel, str(tmp))
    data = load(tmp, default)
    tmp.unlink(missing_ok=True)
    Path(str(tmp) + ".blobsha").unlink(missing_ok=True)
    return data


def put_json(rel, data):
    f = DATA / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    save(f, data)
    _node("push", str(f), rel)


def push_clip_results(run_start, initial_streams, done_request_ids):
    """End of a PC clip run. GitHub keeps changing the shared queue (uploads, statuses) while the PC clips,
    so merge: take the latest shared queue/history and add only what this run produced."""
    local_q = load(QUEUE, [])
    new = [q for q in local_q if q.get("created", "") >= run_start]
    for q in new:  # videos first, so the queue never points at a missing file
        f = DATA / q["file"]
        if f.exists():
            _node("push", str(f), "media/" + q["file"])
            q["stored"] = True
    remote_q = remote_json("auto/queue.json", [])
    ids = {q["id"] for q in new}
    merged = [q for q in remote_q if q["id"] not in ids] + new
    save(QUEUE, merged)
    _node("push", str(QUEUE), "auto/queue.json")

    local_s = load(STATE, {"streams": {}, "runs": []})
    remote_s = remote_json("auto/state.json", {"streams": {}, "runs": []})
    for vid, s in local_s.get("streams", {}).items():
        if s != initial_streams.get(vid):
            remote_s.setdefault("streams", {})[vid] = s
    remote_s["runs"] = (remote_s.get("runs", []) + [r for r in local_s.get("runs", []) if r.get("started", "") >= run_start])[-50:]
    save(STATE, remote_s)
    _node("push", str(STATE), "auto/state.json")

    if done_request_ids:
        reqs = remote_json(REQUESTS, [])
        put_json(REQUESTS, [r for r in reqs if r.get("id") not in done_request_ids])
    log(f"cloud: merged {len(new)} new shorts into the shared queue")


if __name__ == "__main__":  # python cloudstate.py pull|push  (used by the workflows)
    {"pull": pull_state, "push": push_state}[sys.argv[1]]()
