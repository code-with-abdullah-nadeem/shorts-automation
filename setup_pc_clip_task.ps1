# Registers "Shorts - clip on PC": runs auto\pc_clip.ps1 every hour.
# Wakes the laptop from sleep to run, but only when it's plugged in (no battery drain).
$script = Join-Path $PSScriptRoot "pc_clip.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours((Get-Date).Hour + 1) -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -DontStopOnIdleEnd `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 5)
$settings.DisallowStartIfOnBatteries = $true
$settings.StopIfGoingOnBatteries = $false
Register-ScheduledTask -TaskName "Shorts - clip on PC" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName "Shorts - clip on PC" | Select-Object TaskName, State
