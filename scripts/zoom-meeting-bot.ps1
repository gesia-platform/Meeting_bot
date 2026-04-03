param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$ErrorActionPreference = "Stop"

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
