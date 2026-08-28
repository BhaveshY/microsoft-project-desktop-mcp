[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunScript = Join-Path $PSScriptRoot "run-ms-project-mcp.ps1"
$VerifyRoot = Join-Path $RepoRoot (".tmp\mpv-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
$StateRoot = Join-Path $VerifyRoot "state"
$WheelRoot = Join-Path $VerifyRoot "wheel"
New-Item -ItemType Directory -Force -Path $StateRoot, $WheelRoot *> $null

$PreviousStateDir = $env:MSP_MCP_STATE_DIR
$PreviousBackend = $env:MSP_MCP_BACKEND
$env:MSP_MCP_STATE_DIR = $StateRoot
$env:MSP_MCP_BACKEND = "auto"

function Invoke-Checked {
    param(
        [string]$Label,
        [scriptblock]$Command
    )
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

try {
    Invoke-Checked "setup-only bootstrap" { & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $RunScript -SetupOnly }
    $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        $Python = Join-Path $StateRoot "venv\Scripts\python.exe"
    }
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Microsoft Project MCP Python environment was not created"
    }

    Invoke-Checked "compile" { & $Python -m compileall -q (Join-Path $RepoRoot "ms_project_mcp") (Join-Path $RepoRoot "tests\ms_project_mcp") }
    Invoke-Checked "focused tests" { & $Python -m unittest discover -s (Join-Path $RepoRoot "tests\ms_project_mcp") -t $RepoRoot }

    $Config = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot ".mcp.json") | ConvertFrom-Json
    if (-not $Config.mcpServers.'microsoft-project' -or @($Config.mcpServers.PSObject.Properties).Count -ne 1) {
        throw ".mcp.json must contain only the microsoft-project server"
    }

    Invoke-Checked "wheel build" {
        & $Python -m pip wheel --no-deps --no-build-isolation --disable-pip-version-check --cache-dir (Join-Path $VerifyRoot "pip-cache") --wheel-dir $WheelRoot $RepoRoot
    }
    $Wheel = Get-ChildItem -LiteralPath $WheelRoot -Filter "*.whl" | Select-Object -First 1
    if (-not $Wheel) {
        throw "wheel build produced no artifact"
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Archive = [System.IO.Compression.ZipFile]::OpenRead($Wheel.FullName)
    try {
        $Names = @($Archive.Entries | ForEach-Object FullName)
        if (-not ($Names -contains "ms_project_mcp/server.py")) {
            throw "wheel does not contain ms_project_mcp/server.py"
        }
        $EntryPoints = $Archive.Entries | Where-Object FullName -Like "*.dist-info/entry_points.txt" | Select-Object -First 1
        if (-not $EntryPoints) {
            throw "wheel has no entry_points.txt"
        }
        $Reader = [System.IO.StreamReader]::new($EntryPoints.Open())
        try {
            $EntryText = $Reader.ReadToEnd()
        } finally {
            $Reader.Dispose()
        }
        if ($EntryText -notmatch "microsoft-project-mcp\s*=\s*ms_project_mcp.server:main") {
            throw "wheel does not expose the microsoft-project-mcp console script"
        }
    } finally {
        $Archive.Dispose()
    }

    $StdioOutput = & $Python (Join-Path $PSScriptRoot "verify-ms-project-stdio.py")
    if ($LASTEXITCODE -ne 0) {
        throw "stdio verification failed with exit code $LASTEXITCODE"
    }
    $Stdio = $StdioOutput | ConvertFrom-Json
    if ($Stdio.status -ne "VERIFIED") {
        throw "stdio verification did not report VERIFIED"
    }

    $GuardOutput = & $Python (Join-Path $PSScriptRoot "smoke-ms-project-desktop.py")
    $GuardExit = $LASTEXITCODE
    if ($GuardExit -ne 3) {
        throw "desktop smoke consent guard returned $GuardExit instead of 3"
    }
    $Guard = $GuardOutput | ConvertFrom-Json
    if ($Guard.status -ne "NOT_VERIFIED" -or $Guard.activation_attempted) {
        throw "desktop smoke consent guard attempted activation"
    }

    [ordered]@{
        status = "VERIFIED"
        focused_tests = "passed"
        wheel = $Wheel.FullName
        stdio = $Stdio.probes
        desktop_smoke = "NOT_VERIFIED (consent guard only)"
        project_activation_attempted = $false
    } | ConvertTo-Json -Depth 8
} finally {
    if ($null -eq $PreviousStateDir) {
        Remove-Item Env:\MSP_MCP_STATE_DIR -ErrorAction SilentlyContinue
    } else {
        $env:MSP_MCP_STATE_DIR = $PreviousStateDir
    }
    if ($null -eq $PreviousBackend) {
        Remove-Item Env:\MSP_MCP_BACKEND -ErrorAction SilentlyContinue
    } else {
        $env:MSP_MCP_BACKEND = $PreviousBackend
    }
}
