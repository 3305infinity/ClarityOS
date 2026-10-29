# Moonwalk Hub launcher (Windows).  Usage:   .\start-hub.ps1            then, in a 2nd window, optionally:  .\windows_helper\start.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "backend")

function Find-Python {
    foreach ($c in @(@("py","-3"), @("python"))) {
        try { & $c[0] @($c | Select-Object -Skip 1) -c "import sys; assert sys.version_info >= (3,10)" 2>$null; if ($LASTEXITCODE -eq 0) { return $c } } catch {}
    }
    return $null
}
$py = Find-Python
if (-not $py) { Write-Host "Python 3.10+ is required: https://www.python.org/downloads/ (tick 'Add to PATH')" -ForegroundColor Red; exit 1 }

if (-not (Test-Path ".venv")) { Write-Host "Creating virtual environment…"; & $py[0] @($py | Select-Object -Skip 1) -m venv .venv }
$vpy = Join-Path (Get-Location) ".venv\Scripts\python.exe"
& $vpy -m pip install --quiet --disable-pip-version-check -r requirements-hub.txt
if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed" -ForegroundColor Red; exit 1 }

# Without this, console output falls back to cp1252 and the ✓/✗ characters in the
# backend's own log lines raise UnicodeEncodeError.
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# Keys can also be entered in the website (Settings → API keys). An optional backend\.env works too: GEMINI_API_KEY=...
& $vpy -m hub @args
