# Hourly clipping on this PC (Task Scheduler: "Shorts - clip on PC"). GitHub does uploads + dashboard.
$root = Split-Path -Parent $PSScriptRoot          # C:\clipping
$data = Join-Path $root "cloud_data"
$env:CLOUD_STATE = "1"
$env:SHORTS_DATA = $data
$env:PYTHONUTF8 = "1"
$env:BLOB_READ_WRITE_TOKEN = ((Get-Content "$root\dashboard\.env.local" | Where-Object { $_ -like "BLOB_READ_WRITE_TOKEN=*" }) -replace '^BLOB_READ_WRITE_TOKEN=', '' -replace '"', '')

New-Item -ItemType Directory -Force "$data\youtube" | Out-Null
Copy-Item "$root\youtube\cookies.txt" "$data\youtube\cookies.txt" -Force   # spare-account cookies for downloads
if (-not (Test-Path "$PSScriptRoot\node_modules\@vercel\blob")) {
  Push-Location $PSScriptRoot; npm install --no-save --silent @vercel/blob@2.8.2; Pop-Location
}
python "$PSScriptRoot\pc_clip.py" *>> "$data\pc_clip.log"
