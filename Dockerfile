# Always-on Shorts worker for Railway (no web server).
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg nodejs npm ca-certificates fonts-dejavu-core tzdata \
 && rm -rf /var/lib/apt/lists/*

# Claude Code CLI: picks the best 30-40s of each clip, writes hooks/titles, scores virality.
# Signed in on the server with CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`).
RUN npm install -g @anthropic-ai/claude-code@2.1.319 && npm cache clean --force

# CPU-only PyTorch (for Demucs music removal), then the rest; versions match the PC setup.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.15.0
RUN pip install --no-cache-dir \
    "yt-dlp[default]==2026.10.26" yt-dlp-ejs==0.8.0 \
    faster-whisper==1.2.1 ctranslate2==4.9.0 av==19.0.2 \
    demucs==4.1.0 soundfile==0.14.0 numpy==2.5.5 \
    opencv-python-headless==5.1.0.95 \
    google-api-python-client==2.202.0 google-auth==2.61.0 google-auth-oauthlib==1.5.0

# Arial Black is used for captions; DejaVu Sans Bold is the Linux stand-in.
ENV SHORTS_DATA=/data \
    HF_HOME=/data/cache/hf \
    TORCH_HOME=/data/cache/torch \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1

WORKDIR /app/auto
COPY . /app/auto

CMD ["python", "worker.py"]
