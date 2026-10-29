# ClarityOS Windows helper launcher.  Run from anywhere:   .\windows_helper\start.ps1  [-AllowShell]
param([switch]$AllowShell, [string]$Hub = "ws://127.0.0.1:8787/helper", [string]$Token = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = $null
foreach ($c in @("py -3", "python")) {
    try { & ($c.Split(" ")[0]) ($c.Split(" ") | Select-Object -Skip 1) -c "import sys; assert sys.version_info >= (3,10)" 2>$null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {}
}
if (-not $py) { Write-Host "Python 3.10+ is required. Install it from https://www.python.org/downloads/ (tick 'Add to PATH')." -ForegroundColor Red; exit 1 }
$exe = $py.Split(" ")[0]; $pre = @($py.Split(" ") | Select-Object -Skip 1)

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment…"
    & $exe @pre -m venv .venv
}
$vpy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $vpy -m pip install --quiet --disable-pip-version-check -r requirements.txt

# Without this, output falls back to cp1252 and the ✓/✗/⛔ characters in the
# helper's own log lines raise UnicodeEncodeError.
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$argsList = @("helper.py", "--hub", $Hub)
if ($Token) { $argsList += @("--token", $Token) }
if ($AllowShell) { $argsList += "--allow-shell" }
& $vpy @argsList
