# One-shot Windows setup. Safe to re-run. Reverse with .\uninstall.ps1
#   powershell -ExecutionPolicy Bypass -File dictate\install.ps1      (or run ./dictate/install.sh from Git Bash)
# Env: DICTATE_MODEL (default base.en)
$ErrorActionPreference = "Stop"
$Here  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root  = Split-Path -Parent $Here
$Venv  = Join-Path $Root ".venv"
$Py    = Join-Path $Venv "Scripts\python.exe"
$Model = if ($env:DICTATE_MODEL) { $env:DICTATE_MODEL } else { "base.en" }
function Say($m)  { Write-Host "`n== $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[fail] $m" -ForegroundColor Red; exit 1 }

Say "1/5 Python environment"
if (-not (Test-Path $Py)) {
    $found = $null
    foreach ($c in @(@{exe="py";args=@("-3.12")}, @{exe="py";args=@("-3.11")}, @{exe="py";args=@("-3")},
                     @{exe="python";args=@()}, @{exe="python3";args=@()})) {
        try {
            & $c.exe @($c.args) -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>$null
            if ($LASTEXITCODE -eq 0) { $found = $c; break }
        } catch {}
    }
    if (-not $found) { Die "no working Python 3.11+ found. Install it from python.org (tick 'Add python.exe to PATH') and re-run." }
    Write-Host "using: $($found.exe) $($found.args -join ' ')"
    & $found.exe @($found.args) -m venv $Venv
    if (-not (Test-Path $Py)) { Die "venv creation failed" }
}
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet -r (Join-Path $Here "requirements.txt")
if ($LASTEXITCODE -ne 0) { Die "pip install failed (see above)" }

if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    Say "NVIDIA GPU found: installing CUDA runtime wheels (falls back to CPU if this fails)"
    & $Py -m pip install --quiet nvidia-cublas-cu12 nvidia-cudnn-cu12
    if ($LASTEXITCODE -ne 0) { Warn "CUDA wheels failed to install; dictation will run on the CPU" }
}

Say "2/5 Model (faster-whisper-$Model)"
$Dest = Join-Path $Root "models\faster-whisper-$Model"
if (Test-Path (Join-Path $Dest "model.bin")) { Write-Host "already downloaded: $Dest" }
else {
    & $Py -c "from huggingface_hub import snapshot_download as s; s('Systran/faster-whisper-$Model', local_dir=r'$Dest')"
    if (-not (Test-Path (Join-Path $Dest "model.bin"))) { Die "model download failed" }
}
if ($Model -ne "base.en") {
    $Conf = Join-Path $env:APPDATA "dictate"; New-Item -ItemType Directory -Force $Conf | Out-Null
    $Cfg = Join-Path $Conf "config.toml"
    if (-not (Test-Path $Cfg) -or -not (Select-String -Path $Cfg -Pattern '^model *=' -Quiet)) {
        Add-Content $Cfg "model = '$Dest'"
    }
}

Say "3/5 Stop any running daemon"
try { & $Py (Join-Path $Here "dictate.py") quit 2>$null | Out-Null } catch {}
Start-Sleep -Seconds 1

Say "4/5 Start at login + start now"
$Startup = [Environment]::GetFolderPath("Startup")
$Lnk = Join-Path $Startup "Dictate.lnk"
$Ws = New-Object -ComObject WScript.Shell
$S = $Ws.CreateShortcut($Lnk)
$S.TargetPath = "powershell.exe"
$S.Arguments = "-NoProfile -WindowStyle Hidden -Command `"Start-Process -WindowStyle Hidden -FilePath '$Py' -ArgumentList '$Here\dictate.py','serve'`""
$S.WorkingDirectory = $Here
$S.Description = "Dictate: local push-to-talk dictation"
$S.Save()
Write-Host "startup shortcut: $Lnk"
Start-Process -WindowStyle Hidden -FilePath $Py -ArgumentList "`"$Here\dictate.py`"", "serve"
Start-Sleep -Seconds 3

Say "5/5 Checks"
& $Py (Join-Path $Here "dictate.py") doctor
Write-Host @"

Done. Click into any text field, press Ctrl+Alt+Space, speak, press it again to stop and type.
  - You hear a beep when listening starts, two when text is typed, a low one on error.
  - Apps running as Administrator ignore typed input from a normal program: run the daemon elevated for those.
  - Command:    $Here\dictate.cmd status | doctor | last | dashboard
  - Dashboard:  $Here\dictate.cmd dashboard     (logs, errors, checks: send me 'Copy diagnostics' if something fails)
  - Change hotkey/device: %APPDATA%\dictate\config.toml   (hotkey = "ctrl+shift+space", device = "cpu")
  - Tap the hotkey to start/stop, or hold it while you talk. Ctrl+Alt+Backspace cancels, Ctrl+Alt+Z removes the
    last dictated text (cancel_hotkey / undo_hotkey in config.toml). A red dot at the top of the screen = listening.
"@
