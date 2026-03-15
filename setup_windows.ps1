# setup_windows.ps1
# Run this once in the opencode-agent folder
# Right-click PowerShell → "Run as Administrator" not needed,
# but if you get execution policy errors run:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

$ErrorActionPreference = "Stop"
$AgentDir = $PSScriptRoot

Write-Host "`n==> [1/3] Checking Python..." -ForegroundColor Cyan
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "Python not found. Install from https://python.org (check 'Add to PATH')" -ForegroundColor Red
    exit 1
}
python --version

Write-Host "`n==> [2/3] Creating virtual environment..." -ForegroundColor Cyan
if (-not (Test-Path "$AgentDir\.venv")) {
    python -m venv "$AgentDir\.venv"
}
& "$AgentDir\.venv\Scripts\pip" install --upgrade pip -q
& "$AgentDir\.venv\Scripts\pip" install `
    "python-telegram-bot[job-queue]>=21.0" `
    "google-genai>=1.0" `
    "pyyaml>=6.0" -q
Write-Host "    Dependencies installed." -ForegroundColor Green

Write-Host "`n==> [3/3] Finding opencode.exe..." -ForegroundColor Cyan
$oc = Get-Command opencode -ErrorAction SilentlyContinue
if ($oc) {
    Write-Host "    Found: $($oc.Source)" -ForegroundColor Green
    Write-Host "    Paste this into config.yaml -> opencode.binary_path:" -ForegroundColor Yellow
    Write-Host "    $($oc.Source)" -ForegroundColor White
} else {
    Write-Host "    opencode not found in PATH." -ForegroundColor Yellow
    Write-Host "    Find it manually: where.exe opencode" -ForegroundColor Yellow
}

Write-Host "`n======================================" -ForegroundColor Green
Write-Host "  Setup done!" -ForegroundColor Green
Write-Host "======================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Edit config.yaml with your tokens and paths"
Write-Host "  2. Edit opencode-provider.json with your Zen API key"
Write-Host "     and place it at: $env:APPDATA\opencode\config.json"
Write-Host "  3. Run the bot: run_local.bat"
Write-Host ""
