"""Automatic clipping: new finished streams -> 30-40s vertical Shorts in the upload queue.

usage:
  python auto/auto_clip.py                  # check channels in config.json for new streams
  python auto/auto_clip.py --url <stream>   # process one specific stream
  python auto/auto_clip.py --url <stream> --force   # even if it was processed before

Steps per stream: download audio -> find loudness peaks -> download those parts of the
video -> find the facecam -> transcribe (Whisper) -> pick the best 30-40s of each and
write hook/title (Claude Code CLI, with a fallback) -> render -> add to auto/queue.json.
"""
import argparse, json, re, shutil, statistics, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, AUTO, WORK, OUTPUT, QUEUE, STATE, LOCK, config, load, save, log, now, running
from render import render_short
from facecam import find_facecam, find_facecam_auto, track_face

YTDLP = [sys.executable, "-m", "yt_dlp", "--js-runtimes", "node", "--no-warnings",
         "--retries", "10", "--fragment-retries", "50", "--retry-sleep", "fragment:exp=1:20", "-N", "4"]
COOKIES = ROOT / "youtube" / "cookies.txt"  # optional: cookies of a spare YouTube account, if YouTube asks to "confirm you're not a bot"
if COOKIES.exists():
    YTDLP += ["--cookies", str(COOKIES)]
BOT_BLOCK = ("not a bot", "content isn’t available", "content isn't available")  # YouTube refusing this server
PRE, POST = 45, 65  # seconds of video kept around each peak
cfg = config()


ENV = {**__import__("os").environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def ytdlp_json(url, *extra):
    out = subprocess.run([*YTDLP, "--skip-download", "-J", *extra, url], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", env=ENV)
    if out.returncode != 0:
        err = [l for l in (out.stderr or "").splitlines() if "ERROR" in l] or (out.stderr or "yt-dlp failed").strip().splitlines()[-1:]
        raise RuntimeError(err[-1] if err else "yt-dlp failed")
    return json.loads(out.stdout)


def download(cmd):
    """Run a yt-dlp download; on failure raise with yt-dlp's own error line (not just the exit code)."""
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV)
    if r.returncode != 0:
        lines = [l for l in (r.stderr or "").splitlines() if "ERROR" in l] or (r.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("download failed: " + (" | ".join(lines)[-400:] or f"exit {r.returncode}"))


class BotBlocked(RuntimeError):
    pass


def is_gta(title):
    t = (title or "").lower()
    return any(k in t for k in cfg.get("title_keywords", [])) if cfg.get("title_keywords") else True


def channel_for(name):
    """Config for a known streamer, or a generic one for a channel found through search."""
    for ch in cfg["channels"]:
        if ch["name"].lower().replace(" ", "") == (name or "").lower().replace(" ", ""):
            return ch
    return {"name": name or "Unknown", "credit": f"Stream: {name}", "tags": [(name or "").lower()], "language": None}


def new_streams(state):
    """Finished GTA 6 streams not clipped yet: from the watched channels, then from YouTube search."""
    seen, found = set(), []

    def consider(ch, e, source):
        vid = e.get("id")
        if not vid or vid in seen:
            return
        seen.add(vid)
        prev = state["streams"].get(vid)
        # retry streams whose earlier run was interrupted ("processing"/"waiting") or failed (max 2 retries)
        if (prev and prev.get("status") in ("done", "skipped")) or e.get("live_status") in ("is_live", "is_upcoming"):
            return
        if prev and prev.get("status") == "failed" and prev.get("retries", 0) >= 2:
            return
        if not is_gta(e.get("title")):
            return
        found.append({"ch": ch, "url": f"https://www.youtube.com/watch?v={vid}", "views": e.get("view_count") or 0,
                      "source": source, "title": e.get("title")})

    for ch in cfg["channels"]:
        try:
            pl = ytdlp_json(ch["streams_url"], "--flat-playlist", "--playlist-end", str(cfg.get("look_back", 5)))
        except BotBlocked:
            raise
        except Exception as e:
            log(f"could not list {ch['name']}: {e}")
            continue
        for e in pl.get("entries", []):
            consider(ch, e, "channel")

    s = cfg.get("search", {})
    if s.get("enabled"):
        for q in s.get("queries", []):
            try:
                from urllib.parse import quote_plus  # YouTube search sorted by upload date (sp=CAI%3D)
                pl = ytdlp_json(f"https://www.youtube.com/results?search_query={quote_plus(q)}&sp=CAI%253D",
                                "--flat-playlist", "--playlist-end", str(s.get("per_query", 20)))
            except Exception as e:
                log(f"search '{q}' failed: {e}")
                continue
            for e in pl.get("entries", []):
                dur = e.get("duration") or 0
                if dur < s.get("min_duration_minutes", 60) * 60 or (e.get("view_count") or 0) < s.get("min_views", 100000):
                    continue
                consider(channel_for(e.get("channel") or e.get("uploader")), e, "search")

    found.sort(key=lambda f: (f["source"] != "channel", -f["views"]))  # watched streamers first, then most viewed
    if found:
        log(f"found {len(found)} unclipped GTA 6 streams: " + "; ".join(f"{f['ch']['name']}: {f['title'][:40]}" for f in found[:5]))
    return [(f["ch"], f["url"]) for f in found]


def loud_peaks(audio, n, skip_s, gap_s=360):
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio), "-vn",
           "-af", "aresample=8000,aformat=channel_layouts=mono,asetnsamples=8000,astats=metadata=1:reset=1,"
                  "ametadata=print:key=lavfi.astats.Overall.RMS_level", "-f", "null", "-"]
    err = subprocess.run(cmd, stderr=subprocess.PIPE, text=True, errors="ignore").stderr
    lv = [-90.0 if v == "-inf" else float(v) for v in re.findall(r"RMS_level=(-?[\d.]+|-inf)", err)]
    W, H = 20, 10
    win = [statistics.mean(lv[i:i + W]) for i in range(0, max(0, len(lv) - W), H)]
    score = [v - statistics.median(win[max(0, i - 60):i + 61]) for i, v in enumerate(win)]
    picked = []
    for i in sorted(range(len(win)), key=lambda i: -score[i]):
        t = i * H + W // 2
        if win[i] < -45 or t < skip_s or t > len(lv) - POST:
            continue
        if all(abs(t - p) > gap_s for p in picked):
            picked.append(t)
        if len(picked) >= n:
            break
    return sorted(picked), len(lv)


INDIC = {"hi", "ur"}  # spoken Hindi/Urdu: captions + titles in Roman-script Hinglish
_WHISPER = []


def whisper(wcfg):
    if not _WHISPER:
        from faster_whisper import WhisperModel
        _WHISPER.append(WhisperModel(wcfg.get("model", "medium"), device="cpu", compute_type="int8"))
    return _WHISPER[0]


def clip_audio(f):
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0


def run_whisper(wcfg, audio, task, language):
    segs, info = whisper(wcfg).transcribe(audio, task=task, language=language, vad_filter=True, word_timestamps=True, beam_size=5)
    return [(round(w.start, 2), round(w.end, 2), w.word) for sg in segs for w in (sg.words or [])], info.language


def transcribe(clips, wcfg, language=None):
    """Words with timestamps per clip. Hindi/Urdu stay as spoken (romanized later), English stays English,
    anything else is translated to English. Sets c["words"] and c["lang"]."""
    for c in clips:
        tf = c["file"].with_suffix(".words.json")
        data = load(tf, None)
        if isinstance(data, list):  # older cache: English translation
            c["words"], c["lang"] = data, "en"
            continue
        if data:
            c["words"], c["lang"] = data["words"], data["lang"]
            continue
        audio = clip_audio(c["file"])
        words, lang = run_whisper(wcfg, audio, "transcribe", language)
        if lang not in INDIC and lang != "en":
            words, _ = run_whisper(wcfg, audio, "translate", lang)
            lang = "en"
        save(tf, {"lang": lang, "words": words})
        c["words"], c["lang"] = words, lang
        log(f"  transcribed {c['file'].name} ({lang}): {len(words)} words")


def english_words(c, wcfg):
    """Fallback when romanizing fails: English translation of the clip."""
    words, _ = run_whisper(wcfg, clip_audio(c["file"]), "translate", c["lang"])
    return words


def romanize(picks, clips):
    """Hindi/Urdu words (Devanagari/Urdu script from Whisper) -> Roman-script Hinglish, word for word, so caption
    timing is kept. One Claude call for all picked windows. Returns {clip_index: words} for the ones that worked."""
    jobs = []
    for p in picks:
        c = clips[int(p["clip"])]
        if c["lang"] in INDIC:
            s, e = float(p["start"]), float(p["end"])
            idx = [i for i, (a, b, w) in enumerate(c["words"]) if b > s and a < e and w.strip()]
            if idx:
                jobs.append((int(p["clip"]), c, idx))
    claude = shutil.which("claude") or shutil.which("claude.cmd")
    if not jobs or not claude:
        return {}
    body = ",\n".join(f'"{k}": ' + json.dumps([c["words"][i][2].strip() for i in idx], ensure_ascii=False)
                     for k, c, idx in jobs)
    prompt = f"""Convert these Hindi/Urdu livestream words to Roman-script Hinglish, the way Indian and Pakistani
YouTubers write captions (e.g. "bhai", "kya kar raha hai", "yaar"). Words that are English (often written in
Devanagari, like "गेम" or "ड्राइव") must come back in normal English spelling ("game", "drive").
Return EXACTLY one output word per input word, same order, same count. Keep punctuation attached to its word.
Reply with ONLY a JSON object with the same keys:
{{
{body}
}}"""
    try:
        out = subprocess.run([claude, "-p", "--model", cfg.get("claude_model", "sonnet"), "--output-format", "text"],
                             input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=600).stdout
        m = re.search(r"\{.*\}", out, re.S)
        res = json.loads(m.group(0)) if m else {}
    except Exception as e:
        log(f"  romanizing failed: {e}")
        return {}
    done = {}
    for k, c, idx in jobs:
        rom = res.get(str(k))
        if isinstance(rom, list) and len(rom) == len(idx):
            w = list(c["words"])
            for i, r in zip(idx, rom):
                w[i] = (w[i][0], w[i][1], " " + str(r).strip())
            done[k] = w
    log(f"  romanized captions for {len(done)}/{len(jobs)} Hindi/Urdu shorts")
    return done


def transcript_text(words):
    """Compact timed transcript: one line per sentence-ish chunk."""
    lines, cur, t0 = [], [], None
    for a, b, w in words:
        if t0 is None:
            t0 = a
        cur.append(w.strip())
        if re.search(r"[.!?]$", w.strip()) or len(cur) >= 14:
            lines.append(f"[{t0:5.1f}] {' '.join(cur)}"); cur, t0 = [], None
    if cur:
        lines.append(f"[{t0:5.1f}] {' '.join(cur)}")
    return "\n".join(lines)


def choose_with_claude(stream_title, clips, lo, hi, keep_max):
    claude = shutil.which("claude") or shutil.which("claude.cmd")
    if not claude:
        return None
    parts = []
    for i, c in enumerate(clips):
        lang = "Hindi/Urdu (Devanagari/Urdu script)" if c.get("lang") in INDIC else "English"
        parts.append(f"### CLIP {i} (length {c['length']:.0f}s, loudest moment at {PRE}s, speech: {lang})\n{transcript_text(c['words']) or '(no speech)'}")
    prompt = f"""You are editing YouTube Shorts from a livestream: "{stream_title}".
Below are candidate clips with timestamped transcripts of the streamer's speech (game dialogue may appear too).
For each clip decide if it makes a strong Short. Pick the best continuous {lo}-{hi} second window: start right where the
moment begins (no slow lead-in), end right after the payoff, and include the loudest moment at ~{PRE}s when it is part of it.
Reject clips that are dead air, menus, sponsor plugs, or have no clear moment. Keep at most {keep_max}, best first.

Write for each kept clip:
- hook: 2-6 words, UPPERCASE, no emoji. Create curiosity about the payoff so people keep watching
  (e.g. "HE DID NOT EXPECT THIS", "WAIT FOR THE ENDING"), true to the clip, not clickbait lies.
- title: under 80 characters incl. hashtags. Name the streamer, say what happens, one emoji at most, then
  " #shorts" plus 1-2 hashtags (e.g. "#gta6 #carryminati").
- LANGUAGE: for clips whose speech is Hindi/Urdu, write hook and title in Roman-script Hinglish as Indian/Pakistani
  YouTubers do (e.g. hook "BHAI YE KYA HO GAYA", title "Carry bhai ne GTA 6 me ye kya kar diya 😭 #shorts #gta6").
  For English clips write English.
- tags: 3-6 short lowercase tags
- scores: rate the chosen window 0-10 on each of
    hook (do the first 3 seconds grab attention?), engagement (reaction, energy, payoff),
    value (funny/surprising/informative), shareability (would people send it to a friend?)
  Be strict: an average moment is a 5.

Reply with ONLY a JSON array, no prose:
[{{"clip": 0, "keep": true, "start": 12.0, "end": 47.0, "hook": "...", "title": "...", "tags": ["..."],
  "scores": {{"hook": 7, "engagement": 8, "value": 6, "shareability": 7}}}}, ...]

{chr(10).join(parts)}"""
    try:
        out = subprocess.run([claude, "-p", "--model", cfg.get("claude_model", "sonnet"), "--output-format", "text"], input=prompt, capture_output=True,
                             text=True, encoding="utf-8", timeout=600).stdout
        m = re.search(r"\[.*\]", out, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        log(f"  claude selection failed: {e}")
        return None


def choose_fallback(clips, lo, hi, keep_max):
    """No Claude: window around the peak, snapped to a word start, ranked by speech density."""
    picks = []
    for i, c in enumerate(clips):
        starts = [a for a, _, _ in c["words"] if PRE - 18 <= a <= PRE - 4] or [PRE - 12]
        s = min(starts, key=lambda a: abs(a - (PRE - 12)))
        e = min(s + (lo + hi) / 2, c["length"])
        n = sum(1 for a, _, _ in c["words"] if s <= a <= e)
        picks.append({"clip": i, "keep": n > 25, "start": s, "end": e, "hook": "", "title": "", "tags": [], "_n": n})
    picks.sort(key=lambda p: -p["_n"])
    return picks[:keep_max]


def process(ch, url, force=False):
    state = load(STATE, {"streams": {}, "runs": []})
    try:
        info = ytdlp_json(url)
    except RuntimeError as e:
        if any(k in str(e) for k in BOT_BLOCK):
            raise BotBlocked(str(e)) from None
        raise
    vid, title = info["id"], info.get("title", "")
    if ch is None:  # a pasted link: use the streamer's settings if we know them
        ch = channel_for(info.get("channel") or info.get("uploader"))
    if info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        log(f"{vid}: still live or processing on YouTube ({info.get('live_status')}), try later")
        return 0
    if state["streams"].get(vid, {}).get("status") == "done" and not force:
        log(f"{vid}: already processed")
        return 0
    retries = state["streams"].get(vid, {}).get("retries", -1) + 1
    wd = WORK / vid
    (wd / "clips").mkdir(parents=True, exist_ok=True)
    log(f"{vid}: {title} ({info.get('duration_string')})")
    state["streams"][vid] = {"title": title, "channel": ch["name"], "started": now(), "status": "processing", "retries": retries}
    save(STATE, state)
    try:
        audio = wd / "audio.m4a"
        if not audio.exists():
            log("  downloading audio")
            download([*YTDLP, "-f", "139/140/bestaudio[ext=m4a]", "-o", str(audio), url])
        peaks, dur = loud_peaks(audio, cfg.get("candidates_per_stream", 10), cfg.get("skip_minutes_start", 10) * 60)
        log(f"  {len(peaks)} candidate moments: {[f'{p // 3600}:{p % 3600 // 60:02}:{p % 60:02}' for p in peaks]}")
        h = cfg.get("max_source_height", 1440)
        missing = [p for p in peaks if not (wd / "clips" / f"c_{p - PRE:06d}.mp4").exists()]
        if missing:
            log(f"  downloading {len(missing)} video sections (<= {h}p)")
            secs = [a for p in missing for a in ("--download-sections", f"*{p - PRE}-{p + POST}")]
            download([*YTDLP, "-f", f"bv*[height<={h}]+ba[ext=m4a]/b[height<={h}]", "--merge-output-format", "mp4",
                            *secs, "--force-keyframes-at-cuts", "-o", str(wd / "clips" / "c_%(section_start)06d.%(ext)s"), url],
                           )
        clips = []
        for p in peaks:
            f = wd / "clips" / f"c_{p - PRE:06d}.mp4"
            if f.exists():
                clips.append({"file": f, "peak_abs": p, "length": PRE + POST})
        fc = ch.get("facecam")
        for c in clips:
            c["cam"] = find_facecam(c["file"], AUTO / fc["template"], fc["offset"], fc["box"], PRE + POST) if fc else None
            if not c["cam"]:  # no template for this streamer (or it missed): find the facecam by its face
                c["cam"] = find_facecam_auto(c["file"], PRE + POST)
        log(f"  facecam found in {sum(1 for c in clips if c['cam'])}/{len(clips)} clips")
        wcfg = cfg.get("whisper", {})
        transcribe(clips, wcfg, ch.get("language", wcfg.get("language")))

        lo, hi, keep_max = cfg.get("short_min_seconds", 30), cfg.get("short_max_seconds", 40), cfg.get("max_shorts_per_stream", 6)
        picks = choose_with_claude(title, clips, lo, hi, keep_max)
        how = "claude"
        if not picks:
            picks, how = choose_fallback(clips, lo, hi, keep_max), "fallback"
        picks = [p for p in picks if p.get("keep") and 0 <= int(p.get("clip", -1)) < len(clips)]
        for p in picks:  # virality score = average of hook / engagement / value / shareability (0-10)
            sc = {k: float(v) for k, v in (p.get("scores") or {}).items() if isinstance(v, (int, float))}
            p["scores"], p["score"] = sc, round(sum(sc.values()) / len(sc), 1) if sc else None
        min_score = cfg.get("min_virality_score", 5.5)
        dropped = [p for p in picks if p["score"] is not None and p["score"] < min_score]
        if dropped:
            log(f"  dropped {len(dropped)} low-scoring moments (< {min_score})")
        picks = sorted((p for p in picks if p not in dropped), key=lambda p: -(p["score"] or 0))[:keep_max]
        log(f"  selected {len(picks)} shorts ({how})")
        roman = romanize(picks, clips)  # Hindi/Urdu speech -> Hinglish captions

        queue = load(QUEUE, [])
        made = 0
        for p in picks:
            c = clips[int(p["clip"])]
            s = max(0.0, float(p["start"]))
            e = min(float(p["end"]), c["length"])
            e = max(min(e, s + hi), min(s + lo, c["length"]))
            sid = f"{vid}_{c['peak_abs']:06d}"
            out = OUTPUT / f"{sid}.mp4"
            log(f"  rendering {sid} ({e - s:.0f}s, score {p.get('score')}) {p.get('hook', '')}")
            track = None if c["cam"] else track_face(c["file"], s, e - s)  # no facecam: follow the face (IRL cams)
            styles = cfg.get("caption_styles", ["karaoke", "punch"])
            style = styles[made % len(styles)]
            words = roman.get(int(p["clip"])) or c["words"]
            if c.get("lang") in INDIC and int(p["clip"]) not in roman:  # couldn't romanize: English captions instead
                words = english_words(c, wcfg)
            render_short(c["file"], out, s, e - s, c["cam"], words, p.get("hook", ""), peak=PRE,
                         track=track, caption_style=style)
            t_title = p.get("title") or f"{title[:55]} moment #shorts"
            queue = [q for q in queue if q["id"] != sid]
            queue.append({
                "id": sid, "stream_id": vid, "stream_title": title, "channel": ch["name"],
                "stream_time": c["peak_abs"], "file": out.relative_to(ROOT).as_posix(), "title": t_title[:100],
                "hook": p.get("hook", ""), "tags": list(dict.fromkeys(ch.get("tags", []) + p.get("tags", [])))[:12],
                "description": f"{t_title}\n\n{ch.get('credit', '')}\nFull stream: https://youtu.be/{vid}\n\n"
                               + " ".join("#" + re.sub(r"\W", "", t) for t in ["shorts", "gta6", ch["name"]] + p.get("tags", [])[:3] if re.sub(r"\W", "", t)),
                "duration": round(e - s, 1), "created": now(), "status": "ready", "error": None, "attempts": 0,
                "yt_id": None, "publish_at": None, "yt": {},
                "score": p.get("score"), "scores": p.get("scores") or {},
                "layout": "facecam" if c["cam"] else "tracked" if track else "full", "caption_style": style,
            })
            save(QUEUE, queue)
            made += 1
        state = load(STATE, {"streams": {}, "runs": []})
        state["streams"][vid].update({"status": "done", "finished": now(), "shorts": made, "selector": how})
        save(STATE, state)
        return made
    except Exception as e:
        state = load(STATE, {"streams": {}, "runs": []})
        msg = str(e)
        if any(k in msg for k in BOT_BLOCK):  # temporary YouTube block: retry next run without using up a retry
            state["streams"][vid].update({"status": "waiting", "error": "YouTube is blocking downloads for now (bot check). Retrying on the next run.",
                                          "retries": retries - 1, "finished": now()})
            save(STATE, state)
            log("  YouTube download blocked (bot check); will retry next run")
            return 0
        state["streams"][vid].update({"status": "failed", "error": str(e)[-500:], "finished": now()})
        save(STATE, state)
        log(f"  FAILED: {e}")
        return 0


def main():
    global log
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--trigger", default="schedule")
    a = ap.parse_args()
    if running():
        log("another clipping run is in progress; exiting")
        return
    import os
    lock = {"pid": os.getpid(), "started": now(), "trigger": a.trigger, "url": a.url, "step": "starting"}
    save(LOCK, lock)
    plain_log = log

    def log(msg):  # also record the current step for the dashboard
        plain_log(msg)
        lock["step"] = msg.strip()[:160]
        save(LOCK, lock)

    try:
        run_jobs(a)
    finally:
        LOCK.unlink(missing_ok=True)


def run_jobs(a):
    state = load(STATE, {"streams": {}, "runs": []})
    run = {"started": now(), "kind": "clip", "made": 0, "trigger": a.trigger, "url": a.url}
    if a.url:
        jobs = [(None, a.url)]
    else:
        jobs = new_streams(state)[: cfg.get("max_streams_per_run", 1)]
        if not jobs:
            log("no new finished streams")
            run["note"] = "No new finished streams"
    for ch, url in jobs:
        try:
            run["made"] += process(ch, url, a.force or bool(a.url))  # a pasted link is always (re)clipped
        except BotBlocked:
            log("YouTube is blocking downloads (bot check); will retry next run")
            run["note"] = "YouTube blocked downloads (bot check); retrying next run"
    state = load(STATE, {"streams": {}, "runs": []})
    run["finished"] = now()
    state["runs"] = (state.get("runs", []) + [run])[-50:]
    save(STATE, state)


if __name__ == "__main__":
    main()
