"""Locate a streamer's facecam overlay in a 1920x1080 clip by template-matching its banner.

The template is a crop of something fixed on the overlay (Carry's "CarryisLive" banner).
`offset` and `box` describe where the facecam box sits relative to that template at scale 1.
Returns (x, y, w, h) in source pixels, or None when the overlay isn't on screen.
"""
import subprocess
import cv2
import numpy as np


def grab_frames(video, times, w=1920, h=1080):
    frames = []
    for t in times:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                              "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                             capture_output=True).stdout
        if len(raw) == w * h:
            frames.append(np.frombuffer(raw, np.uint8).reshape(h, w))
    return frames


_YUNET = None
MODEL = __import__("pathlib").Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"


def _faces(gray):
    """Face boxes (x, y, w, h) in 1920x1080 coordinates, using OpenCV's YuNet detector."""
    global _YUNET
    if _YUNET is None:
        _YUNET = cv2.FaceDetectorYN.create(str(MODEL), "", (1920, 1080), score_threshold=0.75)
    _, faces = _YUNET.detect(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR))
    return [tuple(float(v) for v in f[:4]) for f in (faces if faces is not None else [])]


def find_facecam_auto(video, duration=110, frames=16, min_share=0.35):
    """Any streamer: find a face that stays in one place across the clip (the facecam),
    then frame a 4:3 box around it. Faces in gameplay move around, so they don't cluster.
    Returns (x, y, w, h) in 1920x1080 coordinates, or None (no facecam / full-screen camera)."""
    shots = grab_frames(video, [duration * (i + 0.5) / frames for i in range(frames)])
    dets = [(f, d) for f, img in enumerate(shots) for d in _faces(img)]
    if not dets:
        return None
    best = []
    for _, (x, y, w, h) in dets:  # group detections whose centres sit within ~3% of the frame
        cx, cy = x + w / 2, y + h / 2
        grp = [d for d in dets if abs(d[1][0] + d[1][2] / 2 - cx) < 60 and abs(d[1][1] + d[1][3] / 2 - cy) < 60]
        if len({f for f, _ in grp}) > len({f for f, _ in best}):
            best = grp
    if len({f for f, _ in best}) < max(3, min_share * len(shots)):
        return None
    fw = float(np.median([d[1][2] for d in best]))
    cx = float(np.median([d[1][0] + d[1][2] / 2 for d in best]))
    cy = float(np.median([d[1][1] + d[1][3] / 2 for d in best]))
    if fw > 300:  # face fills the screen: an IRL / just-chatting camera, not an overlay
        return None
    w = min(fw * 5.8, 760)
    h = w * 0.75
    x = min(max(cx - w / 2, 0), 1920 - w)
    y = min(max(cy - h * 0.42, 0), 1080 - h)
    ev = lambda v: int(round(v / 2)) * 2
    return ev(x), ev(y), ev(w), ev(h)


def track_face(video, start, length, step=0.5, min_face=120):
    """For streams with no facecam overlay (IRL / full-camera): follow the main face so a 9:16 crop
    can pan with it. Returns [(t, cx), ...] (t relative to `start`, cx in 1920-wide coordinates),
    smoothed, or None when no big face is visible most of the time."""
    times = [start + i * step for i in range(int(length / step) + 1)]
    pts = []
    for t, img in zip(times, grab_frames(video, times)):
        faces = [f for f in _faces(img) if f[2] >= min_face]
        if faces:
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])  # biggest face = the streamer
            pts.append((t - start, x + w / 2))
    if len(pts) < 0.5 * len(times):
        return None
    # fill gaps, then smooth so the camera pans gently instead of jittering
    xs, out, j = [], [], 0
    for t in [i * step for i in range(len(times))]:
        while j + 1 < len(pts) and pts[j + 1][0] <= t:
            j += 1
        xs.append(pts[j][1])
    k = 5
    for i in range(len(xs)):
        win = xs[max(0, i - k):i + k + 1]
        out.append((i * step, sum(win) / len(win)))
    return out


def find_facecam(video, template_path, offset, box, duration=110, min_score=0.74):
    tpl = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
    hits = []
    for frame in grab_frames(video, [duration * f for f in (0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95)]):
        best = (0, None, None)
        for s in np.arange(0.85, 1.2, 0.015):
            t = cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            r = cv2.matchTemplate(frame, t, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(r)
            if score > best[0]:
                best = (score, loc, s)
        score, loc, s = best
        if score >= min_score:
            hits.append((loc[0] - offset[0] * s, loc[1] - offset[1] * s, s))
    # keep the biggest group of hits that agree (stray matches on game scenery are outliers)
    groups = [[h for h in hits if abs(h[0] - c[0]) < 20 and abs(h[1] - c[1]) < 20] for c in hits]
    hits = max(groups, key=len, default=[])
    if len(hits) < 2:
        return None
    x, y, s = (float(np.median([h[i] for h in hits])) for i in range(3))
    w, h = box[0] * s, box[1] * s
    ev = lambda v: int(round(v / 2)) * 2
    x, y = max(0, ev(x)), max(0, ev(y))
    return x, y, ev(min(w, 1920 - x)), ev(min(h, 1080 - y))
