#!/usr/bin/env bash
# Deploy / update the Shorts worker on the Oracle Always Free server.
# usage (Git Bash, from C:\clipping):  bash auto/deploy_oracle.sh <server-ip> [--seed]
#   --seed  first install only: also copy the current queue + pending Shorts from this PC
# Secrets are copied file-to-file over SSH; they never get printed.
set -euo pipefail
IP="$1"; SEED="${2:-}"
cd "$(dirname "$0")/.."            # C:\clipping
KEY="youtube/ssh/oracle_shorts"
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 ubuntu@$IP"
export MSYS_NO_PATHCONV=1

echo "== 1/5 server packages (Docker)"
$SSH 'command -v docker >/dev/null || (sudo apt-get update -q && sudo apt-get install -y -q docker.io && sudo systemctl enable --now docker)
      sudo mkdir -p /opt/shorts /opt/shorts-data/youtube && sudo chown -R ubuntu:ubuntu /opt/shorts /opt/shorts-data'

echo "== 2/5 upload code"
tar czf - --exclude=auto/work --exclude=auto/output --exclude=auto/thumbs --exclude='auto/__pycache__' \
    --exclude=auto/queue.json --exclude=auto/state.json --exclude=auto/secrets.json --exclude='auto/*.log' \
    --exclude=auto/clip.lock --exclude=auto/schedule.json $( [ "$SEED" = "--seed" ] || echo --exclude=auto/seed ) auto \
  | $SSH 'rm -rf /opt/shorts/auto && tar xzf - -C /opt/shorts'

echo "== 3/5 secrets (YouTube sign-in, cookies, dashboard token, Claude token)"
for f in client_secret.json token_agent.json cookies.txt; do
  [ -f "youtube/$f" ] && $SSH "cat > /opt/shorts-data/youtube/$f && chmod 600 /opt/shorts-data/youtube/$f" < "youtube/$f"
done
python -I -c "
import json, pathlib
s = json.load(open('auto/secrets.json'))
tok = pathlib.Path('youtube/claude_token.txt')
lines = ['AGENT_TOKEN=' + s['agent_token'], 'DASHBOARD_URL=' + json.load(open('auto/config.json'))['dashboard_url'], 'TZ=Asia/Karachi']
if tok.exists(): lines.append('CLAUDE_CODE_OAUTH_TOKEN=' + tok.read_text().strip())
print('\n'.join(lines))" | $SSH 'cat > /opt/shorts/.env && chmod 600 /opt/shorts/.env'

echo "== 4/5 build image (first time takes ~10 min)"
$SSH 'cd /opt/shorts/auto && sudo docker build -q -t shorts-worker . >/dev/null && echo built'

echo "== 5/5 (re)start worker"
$SSH 'sudo docker rm -f shorts-worker >/dev/null 2>&1 || true
      sudo docker run -d --name shorts-worker --restart unless-stopped --env-file /opt/shorts/.env \
        -v /opt/shorts-data:/data shorts-worker >/dev/null && sleep 20 && sudo docker logs --tail 15 shorts-worker'
echo "done"
