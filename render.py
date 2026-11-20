"""Render one vertical Short (1080x1920) from a horizontal clip.

render_short(src, out, start, length, cam, words, hook, peak)
  cam   : (x, y, w, h) facecam box in 1920x1080 coordinates, or None
          -> with a cam: facecam on top, gameplay below
          -> without: full frame in the middle over a blurred, zoomed copy
  words : [(start, end, word), ...] in clip time (Whisper word timestamps)
  peak  : clip-time of the loudest moment (gets a short punch-in), or None
"""
import re, shutil, subprocess, tempfile
from pathlib import Path

YELLOW = r"&H0000E6FF&"
GREEN = r"&H004FFF7C&"
WHITE = r"&H00FFFFFF&"
HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial Black,92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,1,0,1,8,4,8,50,50,{capy},1
Style: Hook,Arial Black,76,&H00FFFFFF,&H00FFFFFF,&H001F1FE0,&H001F1FE0,-1,0,0,0,100,100,1,0,3,18,0,8,60,60,{hooky},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ts(t):
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02}:{t % 60:05.2f}"


def clean(w):
    return w.strip().upper().replace("{", "(").replace("}", ")")


def group_words(words, max_words=3, max_gap=0.45):
    """Split words into short caption lines: break on punctuation, pauses, or length."""
    lines, cur = [], []
    for i, (a, b, w) in enumerate(words):
        if cur and (len(cur) >= max_words or a - cur[-1][1] > max_gap):
            lines.append(cur); cur = []
        cur.append((a, b, w))
        if re.search(r"[.!?,]$", w.strip()):
            lines.append(cur); cur = []
    if cur:
        lines.append(cur)
    return lines


def drop_loops(lines):
    """Whisper sometimes repeats a phrase over and over; keep at most two repeats."""
    out, prev, rep = [], None, 0
    for ln in lines:
        key = " ".join(re.sub(r"[^\w']", "", w).lower() for _, _, w in ln)
        rep = rep + 1 if key == prev else 0
        prev = key
        if rep < 2:
            out.append(ln)
    return out


def caption_events(words, start, length, style="karaoke"):
    words = [(a - start, b - start, w) for a, b, w in words if b > start and a < start + length and w.strip()]
    lines = drop_loops(group_words(words))
    ev = []
    for i, ln in enumerate(lines):
        end = ln[-1][1]
        nxt = lines[i + 1][0][0] if i + 1 < len(lines) else length
        if nxt - end < 1.0:  # hold through short pauses so captions don't flicker
            end = nxt
        shown = [clean(w) for _, _, w in ln]
        for k, (a, b, _) in enumerate(ln):
            b = end if k == len(ln) - 1 else ln[k + 1][0]
            a = max(a, 0)
            if b - a < 0.04:
                continue
            if style == "punch":  # every word punches in: active word bigger + green, the line stays put
                body = " ".join(f"{{\\c{GREEN}\\fscx118\\fscy118\\t(0,90,\\fscx108\\fscy108)}}{x}{{\\c{WHITE}\\fscx100\\fscy100}}"
                                if j == k else x for j, x in enumerate(shown))
                pop = ""
            else:  # karaoke: active word turns yellow, the line pops in once
                body = " ".join(f"{{\\c{YELLOW}}}{x}{{\\c{WHITE}}}" if j == k else x for j, x in enumerate(shown))
                pop = r"{\fscx82\fscy82\t(0,80,\fscx100\fscy100)}" if k == 0 else ""
            ev.append(f"Dialogue: 1,{ts(a)},{ts(min(b, length))},Cap,,0,0,0,,{pop}{body}")
    return ev


def track_expr(track, cw):
    """ffmpeg expression for the crop's x position following the face (piecewise linear, 1 keyframe/s)."""
    keys = track[::2] if len(track) > 2 else track
    xs = [(t, max(0.0, min(1920 - cw, cx - cw / 2))) for t, cx in keys]
    expr = f"{xs[-1][1]:.1f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(xs, xs[1:]))):
        expr = f"if(lt(t,{t1:.2f}),{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.2f})/{max(t1 - t0, 0.01):.2f},{expr})"
    return expr


def render_short(src, out, start, length, cam, words, hook, peak=None, fps=None, track=None, caption_style="karaoke"):
    if track and not cam:
        capy, hooky = 1300, 330
    elif cam:
        cx, cy, cw, ch = cam
        cam_h = round(1080 * ch / cw / 2) * 2
        game_h = 1920 - cam_h
        game_w = round(1080 * 1080 / game_h / 2) * 2
        capy, hooky = cam_h + 40, 70
    else:
        cam_h, game_h, capy, hooky = 0, 1920, 1250, 330
    z = "1"
    if peak is not None and 1 < peak - start < length - 2.5:
        p = peak - start
        z = f"(1+0.12*between(t,{p - 0.3:.2f},{p + 2.0:.2f}))"
    k = "(iw/1920)"  # source may be 1440p; boxes are in 1080p coordinates
    sharpen = "unsharp=5:5:0.6:5:5:0.0"
    if track and not cam:  # no facecam overlay: a 9:16 window that pans with the streamer's face
        cwid = 1080 * 9 / 16
        vf = (
            f"[0:v]crop=w='trunc({cwid:.0f}*{k}/2)*2':h=ih:x='({track_expr(track, cwid)})*{k}':y=0,"
            f"scale=1080:1920:flags=lanczos,scale=w='trunc(1080*{z}/2)*2':h='trunc(1920*{z}/2)*2':eval=frame,"
            f"crop=1080:1920:(iw-1080)/2:(ih-1920)/2,{sharpen},setsar=1"
        )
    elif cam:
        vf = (
            "[0:v]split[a][b];"
            f"[a]crop=trunc({cw}*{k}/2)*2:trunc({ch}*{k}/2)*2:{cx}*{k}:{cy}*{k},scale=1080:{cam_h}:flags=lanczos,{sharpen}[cam];"
            f"[b]crop=trunc({game_w}*{k}/2)*2:ih:(iw-trunc({game_w}*{k}/2)*2)/2:0,scale=1080:{game_h}:flags=lanczos,"
            f"scale=w='trunc(1080*{z}/2)*2':h='trunc({game_h}*{z}/2)*2':eval=frame,"
            f"crop=1080:{game_h}:(iw-1080)/2:(ih-{game_h})/2,setsar=1[game];"
            "[cam][game]vstack,setsar=1"
        )
    else:
        vf = (
            "[0:v]split[a][b];"
            "[a]scale=-2:1920,crop=1080:1920,boxblur=30:2,eq=brightness=-0.18[bg];"
            f"[b]scale=1080:-2:flags=lanczos,scale=w='trunc(1080*{z}/2)*2':h=-2:eval=frame,crop=1080:ih,{sharpen}[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2-60,setsar=1"
        )
    vf += (f",drawbox=x=0:y=ih-14:w='iw*t/{length}':h=14:color=0xFFE600@0.95:t=fill,"
           "subtitles=subs.ass[v]")
    ass = HEAD.replace("{capy}", str(capy)).replace("{hooky}", str(hooky))
    if hook:
        ass += f"Dialogue: 2,{ts(0)},{ts(3.5)},Hook,,0,0,0,,{{\\fad(100,250)\\fscx70\\fscy70\\t(0,130,\\fscx100\\fscy100)}}{clean(hook)}\n"
    ass += "\n".join(caption_events(words, start, length, caption_style)) +"\n"
    af = "loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=in:d=0.06,afade=t=out:st=%.2f:d=0.25,aresample=48000" % (length - 0.25)
    with tempfile.TemporaryDirectory() as td:  # libass + Windows drive paths: run inside a plain temp dir
        tdp = Path(td)
        (tdp / "subs.ass").write_text(ass, encoding="utf-8")
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
               "-i", str(Path(src).resolve()), "-filter_complex", vf, "-map", "[v]", "-map", "0:a", "-af", af,
               "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-profile:v", "high", "-maxrate", "16M", "-bufsize", "32M",
               *(["-r", str(fps)] if fps else []), "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "out.mp4"]
        subprocess.run(cmd, cwd=td, check=True)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(tdp / "out.mp4", out)
