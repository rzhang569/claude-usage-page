# Removes everything install.ps1 added. Claude Code's own files are never touched.
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1
foreach ($task in "claude-usage-page-build", "claude-usage-page-serve") {
  try { Stop-ScheduledTask -TaskName $task -ErrorAction Stop } catch {}
  try { Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction Stop } catch {}
}
Start-Sleep -Seconds 1
foreach ($dir in ".claude-usage-page", ".claude\skills\my-usage") {
  Remove-Item -Recurse -Force (Join-Path $env:USERPROFILE $dir) -ErrorAction SilentlyContinue
}
Write-Host "Removed."
