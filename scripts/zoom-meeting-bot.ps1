param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
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
$VenvPython = Join-Path $RepoRoot ".venv\\Scripts\\python.exe"
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { "python" }

$env:PYTHONPATH = Join-Path $RepoRoot "src"

Push-Location $RepoRoot
try {
    & $PythonExe -m zoom_meeting_bot_cli @CliArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
