[CmdletBinding()]
param(
    [switch]$AllowWriteFixture
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    [Console]::Error.WriteLine("Run scripts\run-ms-project-mcp.ps1 -SetupOnly first.")
    exit 2
}

$Arguments = @((Join-Path $PSScriptRoot "smoke-ms-project-desktop.py"))
if ($AllowWriteFixture) {
    $Arguments += "--allow-write-fixture"
}
& $Python @Arguments
exit $LASTEXITCODE
