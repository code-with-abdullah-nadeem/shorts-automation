# Registers the two Windows scheduled tasks for the Shorts automation.
#   "Shorts - clip streams"  : every 6 hours, turns new finished streams into Shorts
#   "Shorts - upload & sync" : every 15 minutes, uploads due Shorts and syncs the dashboard
# Run from C:\clipping:   powershell -ExecutionPolicy Bypass -File auto\setup_tasks.ps1

$root = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python).Source

$clip = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$python`" auto\auto_clip.py >> auto\clip_task.log 2>&1`"" -WorkingDirectory $root
$sync = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$python`" auto\agent.py >> auto\agent_task.log 2>&1`"" -WorkingDirectory $root

$every6h = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(1) -RepetitionInterval (New-TimeSpan -Hours 6)
$every15 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 5)

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 5)

Register-ScheduledTask -TaskName "Shorts - clip streams" -Action $clip -Trigger $every6h -Settings $settings -Force | Out-Null
Register-ScheduledTask -TaskName "Shorts - upload & sync" -Action $sync -Trigger $every15 -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName "Shorts - *" | Select-Object TaskName, State
