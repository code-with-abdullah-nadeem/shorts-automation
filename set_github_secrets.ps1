# Copies the pipeline's secrets from local files into the GitHub repository's encrypted secrets.
# Values are piped straight to gh; nothing is printed.
$gh = "C:\Program Files\GitHub CLI\gh.exe"
$repo = "code-with-abdullah-nadeem/shorts-automation"
$root = "C:\clipping"

Get-Content "$root\youtube\client_secret.json" -Raw | & $gh secret set YT_CLIENT_SECRET_JSON --repo $repo
Get-Content "$root\youtube\token_agent.json" -Raw | & $gh secret set YT_TOKEN_JSON --repo $repo
(Get-Content "$root\auto\secrets.json" -Raw | ConvertFrom-Json).agent_token | & $gh secret set AGENT_TOKEN --repo $repo
$blob = (Get-Content "$root\dashboard\.env.local" | Where-Object { $_ -like "BLOB_READ_WRITE_TOKEN=*" }) -replace '^BLOB_READ_WRITE_TOKEN=', '' -replace '"', ''
$blob | & $gh secret set BLOB_READ_WRITE_TOKEN --repo $repo
if (Test-Path "$root\youtube\claude_token.txt") {
  (Get-Content "$root\youtube\claude_token.txt" -Raw).Trim() | & $gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo $repo
}
& $gh variable set DASHBOARD_URL --body "https://shorts-dashboard-ten.vercel.app" --repo $repo
& $gh secret list --repo $repo
