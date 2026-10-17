<#
  Starts the ProofLine backend (FastAPI, port 8000) and the frontend (Vite, port 5173) together.
  Usage (repo root):  powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 [-BackendPort 8000] [-FrontendPort 5173]
  Ctrl+C stops both.
#>
param(
  [int]$BackendPort = 8000,
  [int]$FrontendPort = 5173,
  [switch]$NoReload
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { throw "Python 3.11+ not found on PATH" }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "npm (Node 18+) not found on PATH" }
if (-not (Test-Path (Join-Path $root "frontend\node_modules"))) {
  Write-Host "Installing frontend dependencies..." -ForegroundColor Cyan
  Push-Location (Join-Path $root "frontend"); npm install; Pop-Location
}
$reload = if ($NoReload) { @() } else { @("--reload") }
$backendArgs = @("-m", "uvicorn", "mayday.api.app:app", "--port", "$BackendPort") + $reload
$env:MAYDAY_BACKEND = "http://localhost:$BackendPort"
$env:MAYDAY_PORT = "$FrontendPort"
Write-Host "Backend  -> http://localhost:$BackendPort  (health: /api/health)" -ForegroundColor Green
Write-Host "Frontend -> http://localhost:$FrontendPort" -ForegroundColor Green
$be = Start-Process -FilePath $py -ArgumentList $backendArgs -WorkingDirectory (Join-Path $root "backend") -NoNewWindow -PassThru
try {
  $fe = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npm run dev" -WorkingDirectory (Join-Path $root "frontend") -NoNewWindow -PassThru
  while (-not $be.HasExited -and -not $fe.HasExited) { Start-Sleep -Milliseconds 500 }
} finally {
  foreach ($p in @($fe, $be)) {
    if ($p -and -not $p.HasExited) { & taskkill /PID $p.Id /T /F | Out-Null }
  }
}
