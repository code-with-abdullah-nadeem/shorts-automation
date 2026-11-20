# Re-sign in to YouTube when the sign-in expires (uploads stop / GitHub emails a failed "Sync" run).
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File C:\clipping\auto\reauth_youtube.ps1
# 1) opens the browser to sign in, 2) copies the new sign-in to GitHub secrets and to the private storage the runs read.
$ErrorActionPreference = "Stop"
$root = "C:\clipping"
Remove-Item "$root\youtube\token_agent.json" -ErrorAction SilentlyContinue
python "$root\auto\agent.py" --login

Get-Content "$root\youtube\token_agent.json" -Raw | & "C:\Program Files\GitHub CLI\gh.exe" secret set YT_TOKEN_JSON --repo code-with-abdullah-nadeem/shorts-automation

Copy-Item "$root\auto\blobsync.mjs" "$root\dashboard\blobsync.tmp.mjs"
Push-Location "$root\dashboard"
try { node --env-file=.env.local blobsync.tmp.mjs push "$root\youtube\token_agent.json" youtube/token_agent.json }
finally { Remove-Item blobsync.tmp.mjs; Remove-Item "$root\youtube\token_agent.json.blobsha" -ErrorAction SilentlyContinue; Pop-Location }
Write-Host "Done. The next GitHub sync (within 15 min) uses the new sign-in."
