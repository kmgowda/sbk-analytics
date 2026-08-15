# Self-bootstrapping launcher for sbk-analytics on Windows.
# Run with: powershell -ExecutionPolicy Bypass -File .\sbk-analytics.ps1 <arguments>

Set-StrictMode -Version Latest
# Windows PowerShell 5.1 promotes native stderr to NativeCommandError when this
# is "Stop". Python, pip, Conda, and the analytics CLI legitimately use stderr,
# so native exit codes are checked explicitly instead.
$ErrorActionPreference = "Continue"
$CliArgs = [string[]] @($args)

$MinimumPythonMajor = 3
$MinimumPythonMinor = 9
$SourceRoot = $PSScriptRoot
$EnvironmentHome = if ($env:SBK_ANALYTICS_ENV_HOME) {
    [IO.Path]::GetFullPath($env:SBK_ANALYTICS_ENV_HOME)
} else {
    $SourceRoot
}
$ManagedVenv = Join-Path $EnvironmentHome ".venv"
$ManagedConda = Join-Path $EnvironmentHome ".conda"

function Write-LauncherLog {
    param([string] $Message)
    [Console]::Error.WriteLine("[sbk-analytics] $Message")
}

function Stop-Launcher {
    param([string] $Message)
    Write-LauncherLog "ERROR: $Message"
    exit 1
}

function Resolve-Executable {
    param([string] $Name)
    if (-not $Name) {
        return $null
    }
    if (Test-Path -LiteralPath $Name -PathType Leaf) {
        return [IO.Path]::GetFullPath($Name)
    }
    $command = Get-Command $Name -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $command) {
        return $null
    }
    $pathProperty = $command.PSObject.Properties["Path"]
    if ($pathProperty -and $pathProperty.Value) {
        return $pathProperty.Value
    }
    $sourceProperty = $command.PSObject.Properties["Source"]
    if ($sourceProperty -and $sourceProperty.Value) {
        return $sourceProperty.Value
    }
    return $command.Name
}

function Test-SupportedPython {
    param(
        [string] $Python,
        [string[]] $PrefixArguments = @()
    )
    if (-not $Python -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        return $false
    }
    $arguments = @($PrefixArguments) + @(
        "-c",
        "import sys; raise SystemExit(sys.version_info < ($MinimumPythonMajor, $MinimumPythonMinor))"
    )
    & $Python @arguments *> $null
    return $LASTEXITCODE -eq 0
}

function Find-SystemPython {
    $candidates = @()
    if ($env:SBK_ANALYTICS_PYTHON) {
        $candidates += ,@($env:SBK_ANALYTICS_PYTHON, @())
    }
    $candidates += ,@("python", @())
    $candidates += ,@("python3", @())
    $candidates += ,@("py", @("-3"))

    foreach ($candidate in $candidates) {
        $executable = Resolve-Executable $candidate[0]
        $prefix = [string[]] $candidate[1]
        if (Test-SupportedPython $executable $prefix) {
            return [PSCustomObject]@{
                Executable = $executable
                Prefix = $prefix
            }
        }
    }
    return $null
}

function Get-EnvironmentFingerprint {
    $stream = New-Object IO.MemoryStream
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $rootBytes = [Text.Encoding]::UTF8.GetBytes($SourceRoot)
        $stream.Write($rootBytes, 0, $rootBytes.Length)
        foreach ($name in @(
            "pyproject.toml",
            "requirements.txt",
            "environment.yml",
            "sbk-analytics.ps1"
        )) {
            $path = Join-Path $SourceRoot $name
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                $nameBytes = [Text.Encoding]::UTF8.GetBytes($name)
                $stream.Write($nameBytes, 0, $nameBytes.Length)
                $fileBytes = [IO.File]::ReadAllBytes($path)
                $stream.Write($fileBytes, 0, $fileBytes.Length)
            }
        }
        $hash = $sha256.ComputeHash($stream.ToArray())
        return ([BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

function Test-EnvironmentReady {
    param(
        [string] $Python,
        [string] $EnvironmentRoot
    )
    $marker = Join-Path $EnvironmentRoot ".sbk-analytics-bootstrap"
    $fingerprint = Get-EnvironmentFingerprint
    if (-not $fingerprint -or -not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        return $false
    }
    if ((Get-Content -LiteralPath $marker -Raw).Trim() -ne $fingerprint.Trim()) {
        return $false
    }
    $code = @'
import os
import pathlib
import sys

import analytics
import openpyxl
import openpyxl_image_loader
import PIL
import psutil
import requests
import yaml

source = pathlib.Path(sys.argv[1]).resolve()
module = pathlib.Path(analytics.__file__).resolve()
raise SystemExit(os.path.commonpath((str(source), str(module))) != str(source))
'@
    & $Python -c $code $SourceRoot *> $null
    return $LASTEXITCODE -eq 0
}

function Initialize-Environment {
    param(
        [string] $Python,
        [string] $EnvironmentRoot
    )
    if (Test-EnvironmentReady $Python $EnvironmentRoot) {
        return $true
    }
    Write-LauncherLog "installing sbk-analytics and its Python dependencies into $EnvironmentRoot"
    & $Python -m ensurepip --upgrade *> $null
    & $Python -m pip install --disable-pip-version-check -e $SourceRoot |
        ForEach-Object { [Console]::Error.WriteLine($_) }
    $pipExitCode = $LASTEXITCODE
    if ($pipExitCode -ne 0) {
        return $false
    }
    $fingerprint = Get-EnvironmentFingerprint
    if (-not $fingerprint) {
        return $false
    }
    try {
        Set-Content -LiteralPath (Join-Path $EnvironmentRoot ".sbk-analytics-bootstrap") `
            -Value $fingerprint -Encoding ASCII -ErrorAction Stop
    } catch {
        Write-LauncherLog "could not record environment state: $($_.Exception.Message)"
        return $false
    }
    return $true
}

function Start-Analytics {
    param(
        [ValidateSet("venv", "conda")]
        [string] $Kind,
        [string] $EnvironmentRoot,
        [string] $Python
    )
    if ($Kind -eq "conda") {
        $env:CONDA_PREFIX = $EnvironmentRoot
        Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
        $binaryDirectory = $EnvironmentRoot
    } else {
        $env:VIRTUAL_ENV = $EnvironmentRoot
        Remove-Item Env:CONDA_PREFIX -ErrorAction SilentlyContinue
        $binaryDirectory = Join-Path $EnvironmentRoot "Scripts"
    }
    $env:PATH = "$binaryDirectory;$env:PATH"
    Write-LauncherLog "using $Kind environment: $EnvironmentRoot"
    & $Python -m analytics @CliArgs
    exit $LASTEXITCODE
}

function Use-ExistingEnvironment {
    param(
        [ValidateSet("venv", "conda")]
        [string] $Kind,
        [string] $EnvironmentRoot
    )
    if (-not $EnvironmentRoot) {
        return $false
    }
    $python = if ($Kind -eq "conda") {
        Join-Path $EnvironmentRoot "python.exe"
    } else {
        Join-Path $EnvironmentRoot "Scripts\python.exe"
    }
    if (-not (Test-SupportedPython $python)) {
        return $false
    }
    if (Initialize-Environment $python $EnvironmentRoot) {
        Start-Analytics $Kind $EnvironmentRoot $python
    }
    Write-LauncherLog "could not prepare the existing $Kind environment at $EnvironmentRoot"
    return $false
}

function New-ManagedVenv {
    param([PSCustomObject] $SystemPython)
    New-Item -ItemType Directory -Force -Path $EnvironmentHome `
        -ErrorAction Stop | Out-Null
    Write-LauncherLog "creating Python virtual environment: $ManagedVenv"
    $arguments = @($SystemPython.Prefix) + @("-m", "venv", $ManagedVenv)
    & $SystemPython.Executable @arguments |
        ForEach-Object { [Console]::Error.WriteLine($_) }
    $venvExitCode = $LASTEXITCODE
    if ($venvExitCode -ne 0) {
        return $false
    }
    $python = Join-Path $ManagedVenv "Scripts\python.exe"
    if (-not (Test-SupportedPython $python)) {
        return $false
    }
    if (-not (Initialize-Environment $python $ManagedVenv)) {
        return $false
    }
    Start-Analytics "venv" $ManagedVenv $python
}

function New-ManagedConda {
    $conda = Resolve-Executable "conda"
    if (-not $conda) {
        return $false
    }
    New-Item -ItemType Directory -Force -Path $EnvironmentHome `
        -ErrorAction Stop | Out-Null
    $python = Join-Path $ManagedConda "python.exe"
    if (-not (Test-SupportedPython $python)) {
        Write-LauncherLog "creating fallback Conda environment: $ManagedConda"
        & $conda create --yes --prefix $ManagedConda python=3.10 pip |
            ForEach-Object { [Console]::Error.WriteLine($_) }
        $condaExitCode = $LASTEXITCODE
        if ($condaExitCode -ne 0) {
            return $false
        }
    }
    if (-not (Test-SupportedPython $python)) {
        return $false
    }
    if (-not (Initialize-Environment $python $ManagedConda)) {
        return $false
    }
    Start-Analytics "conda" $ManagedConda $python
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    Stop-Launcher "this launcher supports Windows only"
}

# Prefer environments explicitly activated by the caller.
if ($env:VIRTUAL_ENV) {
    [void] (Use-ExistingEnvironment "venv" $env:VIRTUAL_ENV)
}
if ($env:CONDA_PREFIX) {
    [void] (Use-ExistingEnvironment "conda" $env:CONDA_PREFIX)
}

# Reuse launcher-owned environments before creating anything new.
[void] (Use-ExistingEnvironment "venv" $ManagedVenv)
[void] (Use-ExistingEnvironment "conda" $ManagedConda)

$systemPython = Find-SystemPython
if ($systemPython) {
    if (-not (New-ManagedVenv $systemPython)) {
        Write-LauncherLog "venv setup failed; trying Conda fallback"
    }
}

[void] (New-ManagedConda)

if (-not $systemPython -and -not (Resolve-Executable "conda")) {
    Stop-Launcher "Python 3.9 or newer is required, and Conda is not available to provide it"
}
Stop-Launcher "could not create a working venv or Conda environment; check the installation errors above"
