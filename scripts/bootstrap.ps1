param(
    [switch]$SkipUpgrade
)

$ErrorActionPreference = "Stop"

$Utf8 = New-Object System.Text.UTF8Encoding($false)
& chcp.com 65001 > $null
[Console]::InputEncoding = $Utf8
[Console]::OutputEncoding = $Utf8
$OutputEncoding = $Utf8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

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
    & $PythonExe -m pip install --upgrade pip "setuptools<82" wheel
}

& $PythonExe -m pip install -e $RepoRoot

Write-Host ""
Write-Host "설치가 끝났습니다."
Write-Host "다음 예시:"
Write-Host "  .\\scripts\\zoom-meeting-bot.ps1 quickstart --preset launcher_dm --yes"
Write-Host "  .\\scripts\\zoom-meeting-bot.ps1 create-session \"회의링크\" --passcode \"암호\" --open"
