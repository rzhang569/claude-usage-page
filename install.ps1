# Installs the usage page for the current Windows user:
#   %USERPROFILE%\.claude-usage-page\         the scripts, the built page and its parse cache
#   Task Scheduler                             two tasks: rebuild hourly, serve on 127.0.0.1
#   %USERPROFILE%\.claude\skills\my-usage\     the /my-usage command for Claude Code
# Run again to update. uninstall.ps1 removes all of it. No admin rights needed.
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Port 8788]
param([int]$Port = 8788)
$ErrorActionPreference = "Stop"

$src = $PSScriptRoot
$dest = Join-Path $env:USERPROFILE ".claude-usage-page"
$skill = Join-Path $env:USERPROFILE ".claude\skills\my-usage"
$buildTask = "claude-usage-page-build"
$serveTask = "claude-usage-page-serve"

# A real Python 3, not the Microsoft Store stub that only opens the Store.
$py = $null
foreach ($cand in "py -3", "python", "python3") {
  $parts = $cand.Split(" ")
  $name = $parts[0]
  $rest = @($parts | Select-Object -Skip 1)
  try {
    # Collect all output first: cutting the pipeline short (Select-Object -First) leaves
    # $LASTEXITCODE at -1 in Windows PowerShell 5.1.
    $out = @(& $name @rest -c "import sys; print(sys.executable)" 2>$null)
    $exe = if ($out.Count) { "$($out[0])".Trim() } else { "" }
    if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { $py = $exe; break }
  } catch {}
}
if (-not $py) { throw "Python 3 not found. Install it from python.org, then run this again." }
# pythonw runs the background tasks without a console window.
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }

New-Item -ItemType Directory -Force -Path $dest, $skill | Out-Null
foreach ($f in "build.py", "serve.py", "template.html") { Copy-Item (Join-Path $src $f) $dest -Force }

& $py (Join-Path $dest "build.py")
if ($LASTEXITCODE -ne 0) { throw "build.py failed" }

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$hourly = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)
$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$dest\build.py`"" -WorkingDirectory $dest
Register-ScheduledTask -TaskName $buildTask -Action $action -Trigger @($hourly, $logon) -Settings $settings `
  -Description "Rebuilds the Claude Code usage page every hour" -Force | Out-Null

try { Stop-ScheduledTask -TaskName $serveTask -ErrorAction Stop } catch {}
$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$dest\serve.py`" $Port" -WorkingDirectory $dest
Register-ScheduledTask -TaskName $serveTask -Action $action -Trigger $logon -Settings $settings `
  -Description "Serves the Claude Code usage page on 127.0.0.1:$Port" -Force | Out-Null
Start-ScheduledTask -TaskName $serveTask

# Forward slashes work in both the Git Bash and PowerShell shells Claude Code may use.
$pyFwd = $py -replace '\\', '/'
$buildFwd = "$dest\build.py" -replace '\\', '/'
$skillText = @"
---
name: my-usage
description: Show the local Claude Code usage card (sessions, tokens, heatmap, per-model breakdown). Use only when the user types /my-usage or asks to see their usage card.
---

The card is served at http://127.0.0.1:$Port/ and rebuilt every hour. Keep the turn short.

1. Refresh it so it includes this session: ``"$pyFwd" "$buildFwd"``
2. Open it in the default browser: ``"$pyFwd" -m webbrowser http://127.0.0.1:$Port/``
   If it does not load, run ``powershell -NoProfile -Command "Start-ScheduledTask -TaskName $serveTask"`` and open it again.
   If the user asks to see it beside the chat, open the same URL in the built-in browser pane instead.

Then say in one line that the card is open and when it was updated. Do not restate its numbers unless asked.
If build.py fails, show the last lines of its output and stop.
"@
# UTF-8 without a byte-order mark, so the skill's front matter parses.
[IO.File]::WriteAllText((Join-Path $skill "SKILL.md"), $skillText, (New-Object Text.UTF8Encoding $false))

Write-Host "Installed. Open http://127.0.0.1:$Port/ or type /my-usage in a new Claude Code session"
Write-Host "(sessions that were already open won't list it)."
