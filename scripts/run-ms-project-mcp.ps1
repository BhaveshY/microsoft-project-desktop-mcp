[CmdletBinding()]
param(
    [switch]$SetupOnly
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RequirementsPath = Join-Path $RepoRoot "requirements-ms-project.txt"
$BaseRequirementsPath = Join-Path $RepoRoot "requirements.txt"
$RepoVenvDir = Join-Path $RepoRoot ".venv"
$StateDir = if ($env:MSP_MCP_STATE_DIR) {
    [System.IO.Path]::GetFullPath($env:MSP_MCP_STATE_DIR)
} elseif ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "OpenAI\MicrosoftProjectMCP"
} else {
    Join-Path ([System.IO.Path]::GetTempPath()) "MicrosoftProjectMCP"
}
$FallbackVenvDir = Join-Path $StateDir "venv"
$LogDir = Join-Path $StateDir "logs"
$LogPath = Join-Path $LogDir "bootstrap.log"
$PipCacheDir = Join-Path $StateDir "pip-cache"
$script:VenvDir = $RepoVenvDir
$script:VenvPython = Join-Path $script:VenvDir "Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $LogDir, $PipCacheDir *> $null

function Write-BootstrapLog {
    param([string]$Message)
    Add-Content -LiteralPath $LogPath -Value ("{0} {1}" -f (Get-Date -Format "s"), $Message)
}

function Set-VenvDir {
    param([string]$Path)
    $script:VenvDir = $Path
    $script:VenvPython = Join-Path $Path "Scripts\python.exe"
}

function Test-Python310 {
    param(
        [string]$Command,
        [string[]]$PrefixArgs = @()
    )
    if (-not $Command -or -not (Test-Path -LiteralPath $Command -PathType Leaf)) {
        return $false
    }
    try {
        & $Command @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Get-HostPython {
    $Py = Get-Command py -ErrorAction SilentlyContinue
    if ($Py -and (Test-Python310 -Command $Py.Source -PrefixArgs @("-3"))) {
        return @{ Command = $Py.Source; Args = @("-3") }
    }
    $Python = Get-Command python -ErrorAction SilentlyContinue
    if ($Python -and (Test-Python310 -Command $Python.Source)) {
        return @{ Command = $Python.Source; Args = @() }
    }
    throw "Python 3.10 or newer was not found. See $LogPath"
}

function Invoke-Logged {
    param([scriptblock]$Command)
    & $Command *>> $LogPath
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE. See $LogPath"
    }
}

function New-McpVenv {
    param([string]$Path)
    $HostPython = Get-HostPython
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) *> $null
    Invoke-Logged { & $HostPython.Command @($HostPython.Args + @("-m", "venv", $Path)) }
}

function Ensure-Venv {
    if (Test-Python310 -Command $script:VenvPython) {
        return
    }
    if (Test-Path -LiteralPath $RepoVenvDir) {
        Set-VenvDir -Path $FallbackVenvDir
    }
    if (-not (Test-Python310 -Command $script:VenvPython)) {
        try {
            Write-BootstrapLog "Creating virtual environment at $script:VenvDir"
            New-McpVenv -Path $script:VenvDir
        } catch {
            if ($script:VenvDir -eq $FallbackVenvDir) {
                throw
            }
            Set-VenvDir -Path $FallbackVenvDir
            Write-BootstrapLog "Repo virtual environment was unavailable. Creating $script:VenvDir"
            New-McpVenv -Path $script:VenvDir
        }
    }
    if (-not (Test-Python310 -Command $script:VenvPython)) {
        throw "The Microsoft Project MCP virtual environment requires Python 3.10 or newer."
    }
}

function Get-RequirementsHash {
    $Hashes = @(
        (Get-FileHash -Algorithm SHA256 -LiteralPath $BaseRequirementsPath).Hash,
        (Get-FileHash -Algorithm SHA256 -LiteralPath $RequirementsPath).Hash
    ) -join "`n"
    $Sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $Bytes = [System.Text.Encoding]::UTF8.GetBytes($Hashes)
        return ([System.BitConverter]::ToString($Sha.ComputeHash($Bytes))).Replace("-", "")
    } finally {
        $Sha.Dispose()
    }
}

function Ensure-Requirements {
    $StampPath = Join-Path $script:VenvDir ".requirements-ms-project.sha256"
    $RequiredHash = Get-RequirementsHash
    $InstalledHash = if (Test-Path -LiteralPath $StampPath) {
        (Get-Content -Raw -LiteralPath $StampPath).Trim()
    } else {
        ""
    }
    if ($InstalledHash -ne $RequiredHash) {
        $VersionsReady = $false
        try {
            & $script:VenvPython -c "import importlib.metadata as m,sys; e={'fastmcp':'3.4.7','mcp':'1.29.0','pydantic':'2.13.4','setuptools':'80.9.0','wheel':'0.45.1'}; e.update({'pywin32':'312'} if sys.platform=='win32' else {}); raise SystemExit(0 if all(m.version(k)==v for k,v in e.items()) else 1)" *> $null
            $VersionsReady = ($LASTEXITCODE -eq 0)
        } catch {
            $VersionsReady = $false
        }
        if (-not $VersionsReady) {
            Write-BootstrapLog "Installing Microsoft Project MCP requirements"
            Invoke-Logged {
                & $script:VenvPython -m pip install --disable-pip-version-check --no-input --timeout 15 --retries 1 --cache-dir $PipCacheDir -r $RequirementsPath
            }
        }
        Set-Content -LiteralPath $StampPath -Value $RequiredHash -Encoding ASCII
    }
    & $script:VenvPython -c "import fastmcp, mcp, pydantic" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Microsoft Project MCP dependencies are unavailable. See $LogPath"
    }
}

try {
    Ensure-Venv
    Ensure-Requirements
} catch {
    $Message = ($_ | Out-String).Trim()
    Write-BootstrapLog "Bootstrap failed: $Message"
    [Console]::Error.WriteLine("Microsoft Project MCP bootstrap failed: $Message")
    exit 1
}

if ($SetupOnly) {
    exit 0
}

$PreviousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = if ($PreviousPythonPath) {
    "$RepoRoot$([System.IO.Path]::PathSeparator)$PreviousPythonPath"
} else {
    $RepoRoot
}
try {
    & $script:VenvPython -m ms_project_mcp.server
    $ExitCode = $LASTEXITCODE
} finally {
    if ($null -eq $PreviousPythonPath) {
        Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $PreviousPythonPath
    }
}
exit $ExitCode
