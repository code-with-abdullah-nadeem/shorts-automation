"""Upload + sync agent (run every ~15 min by Task Scheduler).

usage:
  python auto/agent.py --login      # one-time: sign in to YouTube (opens browser)
  python auto/agent.py              # pull dashboard actions, upload due Shorts, refresh statuses, push snapshot
  python auto/agent.py --no-upload  # only sync statuses with YouTube and the dashboard

Dashboard actions: retry (failed -> queued), skip / unskip, publish_now (scheduled -> public now).
"""
import argparse, base64, json, os, re, socket, subprocess, sys, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cloudstate import ensure_media
from common import ROOT, AUTO, DATA_AUTO, QUEUE, STATE, YT_DIR, config, secrets, load, save, log, now, running

SCOPES = ["https://www.googleapis.com/auth/youtube"]
TOKEN = YT_DIR / "token_agent.json"
cfg = config()


def save_queue(queue):
    """Save, keeping any Shorts auto_clip.py added to the file while this agent run was busy."""
    mine = {q["id"] for q in queue}
    queue.extend(x for x in load(QUEUE, []) if x["id"] not in mine)
    save(QUEUE, queue)


# ---------- YouTube auth ----------
def youtube(interactive=False):
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
    if not creds or not creds.valid:
        if not interactive:
            raise SystemExit("Not signed in to YouTube. Run once:  python auto/agent.py --login")
        creds = InstalledAppFlow.from_client_secrets_file(str(YT_DIR / "client_secret.json"), SCOPES).run_local_server(port=0)
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


# ---------- legacy import (the first 11 hand-made shorts) ----------
def import_legacy(queue):
    plan = load(ROOT / "shorts_plan.json", None)
    if not plan or any(q["id"].startswith("legacy_") for q in queue):
        return queue
    uploaded = load(YT_DIR / "uploaded.json", {})
    for s in plan["shorts"]:
        fname = f"{s['clip']}_60s.mp4"
        up = uploaded.get(fname)
        queue.append({
            "id": f"legacy_{s['clip']}", "stream_id": "irNhWMG560k", "stream_title": "CARRYMINATI PLAYS GTA VI | NO PROMOTIONS",
            "channel": "CarryisLive", "stream_time": int(s["clip"].split("_")[1]) + 45,
            "file": (Path(plan["out_dir"]) / fname).as_posix(), "title": s["title"], "hook": s["hook"], "tags": s["tags"],
            "description": f"{s['title']}\n\n{plan.get('credit', '')}\n\n#shorts #gta6 #carryminati", "duration": 60.0,
            "created": "2026-11-19T22:45:00+05:00", "status": "uploaded" if up else "ready", "error": None, "attempts": 0,
            "yt_id": up["id"] if up else None, "publish_at": up["publish_at"] if up else None, "yt": {},
        })
    log(f"imported {len(plan['shorts'])} earlier shorts into the queue")
    return queue


# ---------- dashboard ----------
def dashboard_call(path, payload):
    url, token = cfg.get("dashboard_url", "").rstrip("/"), secrets().get("agent_token")
    if not url or not token:
        return None
    req = urllib.request.Request(url + path, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception as e:
        log(f"dashboard sync failed: {e}")
        return None


def view_status(q):
    yt = q.get("yt") or {}
    if q["status"] in ("replaced", "deleted", "fixing"):
        return q["status"]
    if q["status"] in ("ready", "skipped", "uploading"):
        return {"ready": "queued"}.get(q["status"], q["status"])
    if q["status"] == "failed":
        return "failed"
    if yt.get("upload_status") in ("failed", "rejected", "deleted"):
        return "failed"
    if yt.get("missing"):
        return "removed"
    if yt.get("upload_status") == "uploaded":
        return "processing"
    if yt.get("privacy") == "public":
        return "restricted" if yt.get("issues") else "live"
    pa = yt.get("publish_at") or q.get("publish_at")
    if yt.get("privacy") == "private" and pa and datetime.fromisoformat(pa.replace("Z", "+00:00")) > datetime.now().astimezone():
        return "scheduled"
    if not yt:
        return "scheduled" if q.get("publish_at") else "uploaded"
    return yt.get("privacy", "private")  # private (e.g. locked by an unverified API project) / unlisted


def thumbnail(q):
    """Small vertical poster frame (data URI) for the dashboard, cached in auto/thumbs."""
    src = ROOT / q["file"]
    out = DATA_AUTO / "thumbs" / f"{q['id']}.jpg"
    if not out.exists():
        if not src.exists():
            return None
        out.parent.mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "4", "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=144:256", "-q:v", "6", str(out)], capture_output=True)
        if not out.exists():
            return None
    return "data:image/jpeg;base64," + base64.b64encode(out.read_bytes()).decode()


def snapshot(queue, upload_info):
    state = load(STATE, {"streams": {}, "runs": []})
    items = []
    for q in queue:
        if not q.get("thumb"):  # kept in the queue so runs without the video still have it
            q["thumb"] = thumbnail(q)
        d = dict(q)
        d["file"] = Path(q["file"]).name
        d["view"] = view_status(q)
        items.append(d)
    streams = [{"id": k, **v} for k, v in state.get("streams", {}).items()]
    streams.sort(key=lambda s: s.get("started", ""), reverse=True)
    clip = running()
    if os.environ.get("GITHUB_ACTIONS"):  # cloud: show what the PC is doing (it reports in while clipping)
        st = load(DATA_AUTO / "clip_status.json", {})
        fresh = st.get("updated") and datetime.fromisoformat(st["updated"]) > datetime.now().astimezone() - timedelta(minutes=30)
        clip = st if st.get("running") and fresh else None
    sched = load(DATA_AUTO / "schedule.json", {})  # written by worker.py on a cloud server
    if os.environ.get("CLOUD_STATE") == "1":  # GitHub Actions: clip workflow runs at 00:00, 03:00, ... UTC
        from datetime import timezone
        u = datetime.now(timezone.utc)
        every = max(1, int(cfg.get("clip_every_hours", 1)))
        nxt_utc = u.replace(minute=0, second=0, microsecond=0) + timedelta(hours=every - u.hour % every)
        sched = {"next_clip_run": nxt_utc.astimezone().isoformat()}
    try:  # when the scheduler will next look for new streams on its own
        if sched.get("next_clip_run"):
            raise StopIteration
        nxt = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-ScheduledTaskInfo -TaskName 'Shorts - clip streams').NextRunTime.ToString('o')"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        next_clip = datetime.fromisoformat(nxt[:19]).astimezone().isoformat() if nxt else None
    except StopIteration:
        next_clip = sched["next_clip_run"]
    except Exception:
        next_clip = None
    return {"next_clip_run": next_clip, "generated": now(), "host": "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else socket.gethostname(), "items": items, "streams": streams[:30],
            "runs": state.get("runs", [])[-30:], "upload": {**cfg.get("upload", {}), **upload_info},
            "clipping": {k: clip.get(k) for k in ("started", "trigger", "url", "step")} if clip else None,
            "settings": {"short_seconds": [cfg.get("short_min_seconds", 30), cfg.get("short_max_seconds", 40)],
                         "channels": [c["name"] for c in cfg.get("channels", [])],
                         "clip_every_hours": cfg.get("clip_every_hours", 6), "sync_every_minutes": 15 if os.environ.get("CLOUD_STATE") == "1" else 5}}


# ---------- actions from the dashboard ----------
YT_URL = re.compile(r"^https://(www\.|m\.)?(youtube\.com/(watch\?v=|live/|shorts/)|youtu\.be/)[\w-]{11}([?&#][\w=&%.-]*)?$")


def start_clipping(trigger, url=None):
    """Start auto_clip.py in the background (it keeps running after this agent run ends)."""
    if os.environ.get("GITHUB_ACTIONS"):  # cloud: YouTube blocks GitHub's downloads, so the PC does the clipping
        f = DATA_AUTO / "clip_requests.json"
        reqs = load(f, [])
        if not any(r.get("url") == url for r in reqs):
            reqs.append({"id": f"{now()}-{len(reqs)}", "url": url, "trigger": trigger, "requested": now()})
            save(f, reqs)
        log(f"queued clip request for the PC ({url or 'check for new streams'})")
        return True
    if running():
        log("clipping already running; dashboard request ignored")
        return False
    args = [sys.executable, str(AUTO / "auto_clip.py"), "--trigger", trigger] + (["--url", url] if url else [])
    logf = open(DATA_AUTO / "clip_task.log", "a", encoding="utf-8")
    if os.name != "nt":
        subprocess.Popen(args, cwd=AUTO, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    else:
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(args, cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT, creationflags=flags | 0x01000000)  # + BREAKAWAY_FROM_JOB
        except OSError:
            subprocess.Popen(args, cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT, creationflags=flags)
    log(f"started clipping ({trigger}{': ' + url if url else ''})")
    return True


def apply_actions(queue, actions, yt_factory):
    by_id = {q["id"]: q for q in queue}
    for a in actions or []:
        if a.get("id") == "__pipeline__":
            if a.get("action") == "scan_streams":
                start_clipping("dashboard")
            elif a.get("action") == "clip_url" and YT_URL.match(a.get("url") or ""):
                start_clipping("dashboard", a["url"])
            continue
        q = by_id.get(a.get("id"))
        act = a.get("action")
        if not q:
            continue
        log(f"action {act} on {q['id']}")
        if act == "retry" and view_status(q) in ("failed", "removed", "private"):
            q.update(status="ready", error=None, attempts=0, yt_id=None, publish_at=None, yt={})
        elif act == "skip" and q["status"] in ("ready", "failed"):
            q["status"] = "skipped"
            q["skipped_at"] = now()
        elif act == "unskip" and q["status"] == "skipped":
            q["status"] = "ready"
        elif act == "publish_now" and q.get("yt_id"):
            try:
                set_privacy(yt_factory(), q, "public")
                q["publish_at"] = None
            except Exception as e:
                q["error"] = f"publish now failed: {e}"[:400]
        elif act == "make_private" and q.get("yt_id"):
            try:
                set_privacy(yt_factory(), q, "private")
                q["publish_at"] = None
                q["note"] = "Made private from the dashboard"
            except Exception as e:
                q["error"] = f"make private failed: {e}"[:400]
        elif act == "delete_video" and q.get("yt_id"):
            try:
                yt_factory().videos().delete(id=q["yt_id"]).execute()
                q.update(status="deleted", note=f"Deleted from YouTube ({q['yt_id']})", publish_at=None)
            except Exception as e:
                q["error"] = f"delete failed: {e}"[:400]
        elif act == "fix_music":
            try:
                fixed = remove_music(q, yt_factory if q.get("yt_id") else None)
                queue[:] = [x for x in queue if x["id"] != fixed["id"]] + [fixed]
                save_queue(queue)
            except Exception as e:
                q["status"] = q.get("_prev_status", q["status"]) if q["status"] == "fixing" else q["status"]
                q["error"] = f"remove music failed: {e}"[:400]
                log(f"  {q['error']}")
    return queue


def set_privacy(yt, q, privacy):
    yt.videos().update(part="status", body={"id": q["yt_id"], "status": {
        "privacyStatus": privacy, "selfDeclaredMadeForKids": False}}).execute()
    q.setdefault("yt", {})["privacy"] = privacy


def remove_music(q, yt_factory):
    """Copyright fix: keep only voices (Demucs vocal stem), re-render, queue the clean copy for upload,
    and make the claimed original private. Returns the new queue item."""
    import shutil, tempfile
    src = ensure_media(q)
    if not src:
        raise RuntimeError(f"video file missing: {q['file']}")
    try:
        import demucs  # noqa: F401  (not preinstalled on GitHub runners: install on first use)
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--index-url", "https://download.pytorch.org/whl/cpu",
                        "torch==2.15.0"], check=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "demucs==4.1.0", "soundfile==0.14.0"], check=True)
    q["_prev_status"], q["status"] = q["status"], "fixing"
    new_id = q["id"].split("__nomusic")[0] + "__nomusic"
    out = DATA_AUTO / "output" / f"{new_id}.mp4"
    out.parent.mkdir(exist_ok=True)
    log(f"  separating voices from music for {q['id']}")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "2", "-ar", "44100", str(td / "a.wav")], check=True)
        subprocess.run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "-o", str(td), str(td / "a.wav")],
                       check=True, capture_output=True)
        vocals = next(td.rglob("vocals.wav"))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-i", str(vocals), "-map", "0:v", "-map", "1:a",
                        "-c:v", "copy", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k",
                        "-shortest", "-movflags", "+faststart", str(out)], check=True)
    if q.get("yt_id") and yt_factory:
        try:
            set_privacy(yt_factory(), q, "private")
        except Exception as e:
            log(f"  could not make the original private: {e}")
    q.update(status="replaced", note=f"Replaced by a version without music ({new_id})", publish_at=None)
    q.pop("_prev_status", None)
    log(f"  clean version ready: {out.name}")
    return {**{k: v for k, v in q.items() if k not in ("note", "uploaded")}, "id": new_id,
            "file": out.relative_to(ROOT).as_posix(), "status": "ready", "error": None, "attempts": 0, "yt_id": None,
            "publish_at": None, "yt": {}, "created": now(), "replaces": q["id"], "priority": True}


# ---------- uploading ----------
def slots(queue):
    up = cfg.get("upload", {})
    if up.get("mode") == "asap":
        yield from asap_slots(queue, up)
        return
    times = up.get("times", ["13:00", "19:00"])[: up.get("per_day", 2)]
    taken = {q["publish_at"][:16] for q in queue if q.get("publish_at")}
    t = datetime.now().astimezone() + timedelta(minutes=90)
    day = t.date()
    while True:
        for hm in times:
            hh, mm = map(int, hm.split(":"))
            s = datetime(day.year, day.month, day.day, hh, mm).astimezone()
            if s > t and s.isoformat()[:16] not in taken:
                yield s
        day += timedelta(days=1)


def asap_slots(queue, up):
    """Post as soon as possible: first slot ~20 min out, at least `gap_minutes` apart, at most `max_per_day`
    per calendar day (local time). Clips do best in the first hours after a stream, so don't wait for fixed times."""
    cap = up.get("max_per_day", 6)
    start_h, end_h = up.get("active_hours", [9, 24])  # only post while the audience is awake (local time)
    # spread a day's posts over the active hours (6 posts over 9:00-24:00 = every 2.5 h), never closer than gap_minutes
    gap = max(timedelta(minutes=up.get("gap_minutes", 60)), timedelta(hours=(end_h - start_h) / max(cap, 1)))
    now = datetime.now().astimezone()
    taken = [datetime.fromisoformat(q["publish_at"].replace("Z", "+00:00")).astimezone()
             for q in queue if q.get("publish_at")]
    taken = [x for x in taken if x > now - timedelta(days=2)]
    t = now + timedelta(minutes=20)
    while True:
        day0 = datetime(t.year, t.month, t.day).astimezone()
        if t.hour < start_h:
            t = day0 + timedelta(hours=start_h)
        elif t.hour >= end_h:
            t = day0 + timedelta(days=1, hours=start_h)
            continue
        same_day = sum(1 for x in taken if x.date() == t.date())
        if same_day >= cap:
            t = datetime(t.year, t.month, t.day).astimezone() + timedelta(days=1, hours=start_h)  # next day
            continue
        clash = [x for x in taken if abs(x - t) < gap]
        if clash:
            t = max(clash) + gap
            continue
        taken.append(t)
        yield t
        t += gap


def upload_due(queue, yt):
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError
    up = cfg.get("upload", {})
    ready = [q for q in queue if q["status"] == "ready"]
    ready.sort(key=lambda q: -(q.get("score") or 0))          # best first ...
    ready.sort(key=lambda q: q.get("created", ""), reverse=True)  # ... from the newest stream (clips age fast)
    ready.sort(key=lambda q: not q.get("priority"))           # copyright fixes before everything
    todo = ready[: up.get("max_per_run", 4)]
    gen = slots(queue)
    for q in todo:
        f = ensure_media(q)
        if not f:
            q.update(status="failed", error="Video no longer stored (it was skipped and cleaned up). Use 'Clip a stream' to remake it.")
            continue
        when = next(gen)
        body = {"snippet": {"title": q["title"][:100], "description": q.get("description", ""), "tags": q.get("tags", []),
                            "categoryId": "20"},
                "status": {"privacyStatus": "private", "publishAt": when.isoformat(), "selfDeclaredMadeForKids": False}}
        q["status"] = "uploading"
        save_queue(queue)
        log(f"uploading {q['id']} for {when:%a %d %b %H:%M}")
        try:
            req = yt.videos().insert(part="snippet,status", body=body,
                                     media_body=MediaFileUpload(str(f), mimetype="video/mp4", chunksize=8 << 20, resumable=True))
            resp = None
            while resp is None:
                _, resp = req.next_chunk()
            q.update(status="uploaded", yt_id=resp["id"], publish_at=when.isoformat(), error=None, uploaded=now())
            log(f"  done https://youtube.com/shorts/{resp['id']}")
        except HttpError as e:
            reason = str(e)
            q["attempts"] = q.get("attempts", 0) + 1
            if "quotaExceeded" in reason or "uploadLimitExceeded" in reason:
                q.update(status="ready", error="YouTube daily limit reached, will retry next run")
                log("  quota/upload limit reached; stopping for now")
                break
            q.update(status="failed", error=reason[:400])
            log(f"  failed: {reason[:200]}")
        except Exception as e:
            q["attempts"] = q.get("attempts", 0) + 1
            q.update(status="ready" if q["attempts"] < 3 else "failed", error=str(e)[:400])
            log(f"  failed: {e}")
        save_queue(queue)
    return queue


REJECTION = {
    "claim": "Blocked by a Content ID claim", "copyright": "Removed for copyright infringement",
    "duplicate": "Rejected as a duplicate upload", "inappropriate": "Rejected for inappropriate content",
    "legal": "Removed for legal reasons", "length": "Rejected: too long", "termsOfUse": "Rejected: Terms of Service",
    "trademark": "Removed for trademark infringement", "uploaderAccountClosed": "Channel closed",
    "uploaderAccountSuspended": "Channel suspended",
}


def restrictions(st, cd):
    """Everything the public API reveals about claims/restrictions on a video."""
    issues = []
    if st.get("rejectionReason"):
        r = st["rejectionReason"]
        issues.append({"kind": "copyright" if r in ("claim", "copyright") else "rejected",
                       "text": REJECTION.get(r, f"Rejected: {r}")})
    if st.get("failureReason"):
        issues.append({"kind": "failed", "text": f"Processing failed: {st['failureReason']}"})
    rr = cd.get("regionRestriction") or {}
    if rr.get("blocked"):
        n = len(rr["blocked"])
        issues.append({"kind": "region", "text": f"Blocked in {n} countr{'y' if n == 1 else 'ies'}: {', '.join(rr['blocked'][:12])}{'…' if n > 12 else ''}"
                                                 " (usually set by a Content ID claim; check Copyright in Studio)"})
    if rr.get("allowed") is not None:
        issues.append({"kind": "region", "text": f"Only viewable in {len(rr['allowed'])} countries"})
    if (cd.get("contentRating") or {}).get("ytRating") == "ytAgeRestricted":
        issues.append({"kind": "age", "text": "Age-restricted"})
    return issues


def refresh_status(queue, yt):
    ids = [q["yt_id"] for q in queue if q.get("yt_id")]
    found = {}
    for i in range(0, len(ids), 50):
        r = yt.videos().list(part="status,statistics,contentDetails", id=",".join(ids[i:i + 50])).execute()
        for v in r.get("items", []):
            found[v["id"]] = v
    for q in queue:
        if not q.get("yt_id"):
            continue
        v = found.get(q["yt_id"])
        if not v:
            q["yt"] = {"missing": True, "checked": now()}
            continue
        st, sx, cd = v.get("status", {}), v.get("statistics", {}), v.get("contentDetails", {})
        prev = q.get("yt") or {}
        q["yt"] = {"privacy": st.get("privacyStatus"), "upload_status": st.get("uploadStatus"),
                   "failure": st.get("failureReason"), "rejection": st.get("rejectionReason"),
                   "publish_at": st.get("publishAt"), "views": int(sx.get("viewCount", 0)),
                   "likes": int(sx.get("likeCount", 0)), "comments": int(sx.get("commentCount", 0)),
                   "issues": restrictions(st, cd)}
        old = {k: v for k, v in prev.items() if k != "checked"}
        q["yt"]["checked"] = prev.get("checked") if old == q["yt"] else now()  # only changes when YouTube's data does
    return queue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args()
    if a.login:
        youtube(interactive=True)
        print("Signed in. The agent can now run unattended.")
        return
    queue = import_legacy(load(QUEUE, []))
    for q in queue:  # an upload interrupted mid-way goes back to the queue
        if q["status"] == "uploading":
            q["status"] = "ready"
    _yt = []
    get_yt = lambda: _yt[0] if _yt else (_yt.append(youtube()) or _yt[0])

    resp = dashboard_call("/api/agent/sync", {"snapshot": snapshot(queue, {}), "phase": "start"})
    queue = apply_actions(queue, (resp or {}).get("actions"), get_yt)
    save_queue(queue)
    if not a.no_upload:
        queue = upload_due(queue, get_yt())
    try:
        queue = refresh_status(queue, get_yt())
    except (Exception, SystemExit) as e:  # not signed in yet / network: still push what we know
        log(f"status refresh failed: {e}")
    save_queue(queue)
    nxt = next(slots(queue))
    resp = dashboard_call("/api/agent/sync", {"snapshot": snapshot(queue, {"next_slot": nxt.isoformat()}), "phase": "end"})
    save_queue(queue)  # keeps thumbnails generated for the snapshot
    if resp and resp.get("actions"):  # clicked while we were uploading: handle now
        queue = apply_actions(queue, resp["actions"], get_yt)
        save_queue(queue)
        dashboard_call("/api/agent/sync", {"snapshot": snapshot(queue, {"next_slot": nxt.isoformat()}), "phase": "end"})
    counts = {}
    for q in queue:
        counts[view_status(q)] = counts.get(view_status(q), 0) + 1
    log(f"agent done: {counts}")


if __name__ == "__main__":
    main()
