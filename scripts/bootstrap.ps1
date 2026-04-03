param(
    [switch]$SkipUpgrade
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $RepoRoot ".venv"
$PythonExe = Join-Path $VenvDir "Scripts\\python.exe"

if (-not (Test-Path $PythonExe)) {
    Write-Host "가상환경을 생성합니다: $VenvDir"
    python -m venv $VenvDir
}

if (-not (Test-Path $PythonExe)) {
    throw "가상환경 Python을 찾을 수 없습니다: $PythonExe"
}

if (-not $SkipUpgrade) {
    & $PythonExe -m pip install --upgrade pip setuptools wheel
}

& $PythonExe -m pip install -e $RepoRoot

Write-Host ""
Write-Host "설치가 끝났습니다."
Write-Host "다음 예시:"
Write-Host "  .\\scripts\\zoom-meeting-bot.ps1 setup"
Write-Host "  .\\scripts\\zoom-meeting-bot.ps1 init --preset launcher_dm"
Write-Host "  .\\scripts\\zoom-meeting-bot.ps1 configure"
Write-Host "  .\\scripts\\zoom-meeting-bot.ps1 doctor --mode launcher"
