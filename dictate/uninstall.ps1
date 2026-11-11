# Stops the daemon and removes the startup shortcut. Config, history and models are kept.
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path (Split-Path -Parent $Here) ".venv\Scripts\python.exe"
try { & $Py (Join-Path $Here "dictate.py") quit 2>$null | Out-Null } catch {}
Remove-Item -ErrorAction SilentlyContinue (Join-Path ([Environment]::GetFolderPath("Startup")) "Dictate.lnk")
Write-Host "uninstalled (your config/history in %APPDATA%\dictate and %LOCALAPPDATA%\dictate was kept)"
