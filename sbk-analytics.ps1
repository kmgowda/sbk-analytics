# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Self-contained launcher for sbk-analytics on Windows. It acquires a verified
# uv binary and isolated Python runtime when neither Python nor Conda exists.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"
$CliArgs = [string[]] @($args)
$SourceRoot = $PSScriptRoot
$PolicyPath = Join-Path $SourceRoot "sbk-bootstrap.env"

function Write-LauncherLog {
    param([string] $Message)
    [Console]::Error.WriteLine("[sbk-analytics] $Message")
}

function Stop-Launcher {
    param([string] $Message)
    Write-LauncherLog "ERROR: $Message"
    exit 1
}

if (-not (Test-Path -LiteralPath $PolicyPath -PathType Leaf)) {
    Stop-Launcher "bootstrap policy is missing: $PolicyPath"
}

$Policy = @{}
foreach ($line in Get-Content -LiteralPath $PolicyPath) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
    $parts = $trimmed.Split("=", 2)
    if ($parts.Count -ne 2) {
        Stop-Launcher "invalid bootstrap policy line: $line"
    }
    $Policy[$parts[0].Trim()] = $parts[1].Trim()
}

function Get-PolicyValue {
    param([string] $Name)
    if (-not $Policy.ContainsKey($Name)) {
        Stop-Launcher "invalid bootstrap policy ${Name}: required key is missing"
    }
    $value = [string] $Policy[$Name]
    if ([string]::IsNullOrWhiteSpace($value)) {
        Stop-Launcher "invalid bootstrap policy ${Name}: value must not be empty"
    }
    return $value
}

function Assert-PolicyVersion {
    param([string] $Name, [string] $Value)
    if ($Value -notmatch '^\d+(\.\d+)+$') {
        Stop-Launcher "invalid bootstrap policy ${Name}: expected a dotted numeric version"
    }
}

function Assert-PolicyLeafName {
    param([string] $Name, [string] $Value)
    if ($Value -eq "." -or $Value -eq ".." -or $Value -match '[\\/]') {
        Stop-Launcher "invalid bootstrap policy ${Name}: expected a filename without path separators"
    }
}

$PythonVersion = Get-PolicyValue "SBK_ANALYTICS_PYTHON_VERSION"
$UvVersion = Get-PolicyValue "SBK_ANALYTICS_UV_VERSION"
$RuntimeFolder = Get-PolicyValue "SBK_ANALYTICS_RUNTIME_FOLDER"
$BootstrapMarker = Get-PolicyValue "SBK_ANALYTICS_BOOTSTRAP_MARKER"
Assert-PolicyVersion "SBK_ANALYTICS_PYTHON_VERSION" $PythonVersion
Assert-PolicyVersion "SBK_ANALYTICS_UV_VERSION" $UvVersion
Assert-PolicyLeafName "SBK_ANALYTICS_RUNTIME_FOLDER" $RuntimeFolder
Assert-PolicyLeafName "SBK_ANALYTICS_BOOTSTRAP_MARKER" $BootstrapMarker

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    Stop-Launcher "this launcher supports Windows only"
}

$architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
switch ($architecture) {
    "X64" {
        $UvTarget = "x86_64-pc-windows-msvc"
        $UvArchiveSha256 = Get-PolicyValue "SBK_ANALYTICS_UV_WINDOWS_X86_64_SHA256"
        $PlatformId = "windows-x86_64"
    }
    "Arm64" {
        $UvTarget = "aarch64-pc-windows-msvc"
        $UvArchiveSha256 = Get-PolicyValue "SBK_ANALYTICS_UV_WINDOWS_AARCH64_SHA256"
        $PlatformId = "windows-aarch64"
    }
    default { Stop-Launcher "unsupported processor architecture: $architecture" }
}
if ($UvArchiveSha256 -notmatch '^[0-9a-f]{64}$') {
    Stop-Launcher "invalid bootstrap policy uv checksum: expected 64 lowercase hexadecimal characters"
}

$RuntimeHome = if ($env:SBK_ANALYTICS_ENV_HOME) {
    [IO.Path]::GetFullPath($env:SBK_ANALYTICS_ENV_HOME)
} else {
    $defaultStateRoot = if ($env:LOCALAPPDATA) {
        $env:LOCALAPPDATA
    } else {
        [Environment]::GetFolderPath("LocalApplicationData")
    }
    if ([string]::IsNullOrWhiteSpace($defaultStateRoot)) {
        Stop-Launcher "LOCALAPPDATA or SBK_ANALYTICS_ENV_HOME is required"
    }
    Join-Path $defaultStateRoot $RuntimeFolder
}
$UvCache = Join-Path $RuntimeHome "cache\uv"
$UvPythonRoot = Join-Path $RuntimeHome "python"
$UvToolRoot = Join-Path $RuntimeHome "tools\uv\$UvVersion\$UvTarget"
$UvBinary = Join-Path $UvToolRoot "uv.exe"
$UvBinaryMarker = Join-Path $UvToolRoot "uv.sha256"
$AppRoot = Join-Path $RuntimeHome "app"
$LockRoot = Join-Path $RuntimeHome "locks"
$UvReleaseBase = if ($env:SBK_ANALYTICS_UV_BASE_URL) {
    $env:SBK_ANALYTICS_UV_BASE_URL.TrimEnd("/")
} else {
    "https://github.com/astral-sh/uv/releases/download"
}

function Get-FileSha256 {
    param([string] $Path)
    # Use the .NET primitive directly: Get-FileHash is not present in every
    # supported Windows PowerShell installation (including some CI images).
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try {
        return ([BitConverter]::ToString(
            $algorithm.ComputeHash($stream)
        )).Replace("-", "").ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Get-SourceFingerprint {
    $sha256 = [Security.Cryptography.SHA256]::Create()
    $stream = New-Object IO.MemoryStream
    try {
        $header = "schema=3`npython=$PythonVersion`nuv=$UvVersion`nplatform=$PlatformId`n"
        $headerBytes = [Text.Encoding]::UTF8.GetBytes($header)
        $stream.Write($headerBytes, 0, $headerBytes.Length)
        $files = @(
            "pyproject.toml", "uv.lock", ".python-version", "sbk-bootstrap.env",
            "sbk-config.env", "requirements.txt", "environment.yml", "MANIFEST.in",
            "sbk-analytics", "sbk-analytics.sh", "sbk-analytics.ps1"
        ) | ForEach-Object { Join-Path $SourceRoot $_ }
        $runtimeFolders = @("analytics", "examples") | ForEach-Object {
            Join-Path $SourceRoot $_
        }
        $files += @(Get-ChildItem -LiteralPath $runtimeFolders -Recurse -File |
            Where-Object {
                $_.Extension -eq ".py" -or $_.Extension -eq ".txt" -or
                $_.Extension -eq ".env" -or $_.Extension -eq ".yml" -or
                $_.Extension -eq ".yaml"
            } | Select-Object -ExpandProperty FullName)
        $sourcePrefix = $SourceRoot.TrimEnd([IO.Path]::DirectorySeparatorChar)
        foreach ($path in ($files | Sort-Object)) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                Stop-Launcher "bootstrap input is missing: $path"
            }
            $relative = $path.Substring($sourcePrefix.Length).TrimStart(
                [IO.Path]::DirectorySeparatorChar
            )
            $record = "$relative $(Get-FileSha256 $path)`n"
            $bytes = [Text.Encoding]::UTF8.GetBytes($record)
            $stream.Write($bytes, 0, $bytes.Length)
        }
        return ([BitConverter]::ToString(
            $sha256.ComputeHash($stream.ToArray())
        )).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

function Enter-BootstrapLock {
    param([string] $Name)
    New-Item -ItemType Directory -Force -Path $LockRoot | Out-Null
    $lock = Join-Path $LockRoot "$Name.lock"
    foreach ($attempt in 1..120) {
        try {
            New-Item -ItemType Directory -Path $lock -ErrorAction Stop | Out-Null
            Set-Content -LiteralPath (Join-Path $lock "pid") -Value $PID `
                -Encoding ASCII
            return $lock
        } catch {
            $ownerFile = Join-Path $lock "pid"
            $owner = if (Test-Path -LiteralPath $ownerFile -PathType Leaf) {
                Get-Content -LiteralPath $ownerFile -First 1
            } else { $null }
            if ($owner -match '^\d+$' -and -not (
                Get-Process -Id ([int] $owner) -ErrorAction SilentlyContinue
            )) {
                Remove-Item -LiteralPath $lock -Recurse -Force -ErrorAction SilentlyContinue
                continue
            }
            if ($attempt -eq 1) { Write-LauncherLog "waiting for bootstrap lock: $lock" }
            Start-Sleep -Seconds 1
        }
    }
    Stop-Launcher "timed out waiting for bootstrap lock: $lock"
}

function Exit-BootstrapLock {
    param([string] $Lock)
    if ($Lock) {
        Remove-Item -LiteralPath $Lock -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Test-UvReady {
    if (-not (Test-Path -LiteralPath $UvBinary -PathType Leaf) -or
        -not (Test-Path -LiteralPath $UvBinaryMarker -PathType Leaf)) {
        return $false
    }
    $expected = (Get-Content -LiteralPath $UvBinaryMarker -First 1).Trim()
    if ((Get-FileSha256 $UvBinary) -ne $expected) { return $false }
    & $UvBinary --version *> $null
    return $LASTEXITCODE -eq 0
}

function Get-Uv {
    if ($env:SBK_ANALYTICS_UV_EXECUTABLE) {
        $override = [IO.Path]::GetFullPath($env:SBK_ANALYTICS_UV_EXECUTABLE)
        if (-not (Test-Path -LiteralPath $override -PathType Leaf)) {
            Stop-Launcher "SBK_ANALYTICS_UV_EXECUTABLE does not exist: $override"
        }
        return $override
    }
    if (Test-UvReady) { return $UvBinary }
    $lock = Enter-BootstrapLock "uv-$UvVersion-$UvTarget"
    try {
        if (Test-UvReady) { return $UvBinary }
        $stage = "$UvToolRoot.install-$PID"
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $stage | Out-Null
        $archive = Join-Path $stage "uv.zip"
        $url = "$UvReleaseBase/$UvVersion/uv-$UvTarget.zip"
        $uri = [Uri] $url
        if ($uri.Scheme -ne "https" -and
            $env:SBK_ANALYTICS_BOOTSTRAP_ALLOW_INSECURE -ne "1") {
            Stop-Launcher "bootstrap downloads require HTTPS: $url"
        }
        Write-LauncherLog "downloading verified uv $UvVersion for $PlatformId"
        try {
            Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing `
                -ErrorAction Stop
        } catch {
            Stop-Launcher "could not download uv from ${url}: $($_.Exception.Message)"
        }
        if ((Get-FileSha256 $archive) -ne $UvArchiveSha256) {
            Stop-Launcher "uv archive checksum mismatch for $PlatformId"
        }
        Expand-Archive -LiteralPath $archive -DestinationPath $stage -Force
        $extracted = Get-ChildItem -LiteralPath $stage -Recurse -File `
            -Filter "uv.exe" | Select-Object -First 1
        if (-not $extracted) { Stop-Launcher "uv archive did not contain uv.exe" }
        $publish = Join-Path $stage "publish"
        New-Item -ItemType Directory -Force -Path $publish | Out-Null
        $publishedUv = Join-Path $publish "uv.exe"
        Copy-Item -LiteralPath $extracted.FullName -Destination $publishedUv
        Set-Content -LiteralPath (Join-Path $publish "uv.sha256") `
            -Value (Get-FileSha256 $publishedUv) `
            -Encoding ASCII
        & $publishedUv --version *> $null
        if ($LASTEXITCODE -ne 0) {
            Stop-Launcher "downloaded uv failed its health check"
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $UvToolRoot) |
            Out-Null
        Remove-Item -LiteralPath $UvToolRoot -Recurse -Force `
            -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $publish -Destination $UvToolRoot
        Remove-Item -LiteralPath $stage -Recurse -Force
    } finally {
        Exit-BootstrapLock $lock
    }
    if (-not (Test-UvReady)) { Stop-Launcher "installed uv failed its health check" }
    return $UvBinary
}

function Get-AppPython {
    param([string] $EnvironmentRoot)
    return Join-Path $EnvironmentRoot "Scripts\python.exe"
}

function Test-AppReady {
    param([string] $EnvironmentRoot, [string] $Fingerprint)
    $python = Get-AppPython $EnvironmentRoot
    $marker = Join-Path $EnvironmentRoot $BootstrapMarker
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
        -not (Test-Path -LiteralPath $marker -PathType Leaf)) { return $false }
    if ((Get-Content -LiteralPath $marker -First 1).Trim() -ne $Fingerprint) {
        return $false
    }
    $code = @'
import pathlib, sys
import analytics, openpyxl, openpyxl_image_loader, PIL, psutil, requests, yaml
root = pathlib.Path(sys.prefix).resolve()
module = pathlib.Path(analytics.__file__).resolve()
raise SystemExit(root not in module.parents)
'@
    $oldPythonPath = $env:PYTHONPATH
    $oldPythonHome = $env:PYTHONHOME
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    & $python -P -c $code *> $null
    if ($null -ne $oldPythonPath) { $env:PYTHONPATH = $oldPythonPath }
    if ($null -ne $oldPythonHome) { $env:PYTHONHOME = $oldPythonHome }
    return $LASTEXITCODE -eq 0
}

function Invoke-Uv {
    param([string] $Uv, [string[]] $Arguments)
    & $Uv @Arguments *>&1 | ForEach-Object { [Console]::Error.WriteLine($_) }
    return $LASTEXITCODE -eq 0
}

function Initialize-Application {
    param([string] $Uv, [string] $Fingerprint, [string] $EnvironmentRoot)
    $lock = Enter-BootstrapLock "app-$Fingerprint"
    $stage = Join-Path $AppRoot ".$Fingerprint.install-$PID"
    try {
        if (Test-AppReady $EnvironmentRoot $Fingerprint) { return }
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $AppRoot, $UvCache, $UvPythonRoot |
            Out-Null
        $env:UV_CACHE_DIR = $UvCache
        $env:UV_PYTHON_INSTALL_DIR = $UvPythonRoot
        $offline = if ($env:SBK_ANALYTICS_BOOTSTRAP_OFFLINE -eq "1") {
            @("--offline")
        } else { @() }
        Write-LauncherLog "preparing isolated Python $PythonVersion runtime"
        if (-not (Invoke-Uv $Uv (@(
            "python", "install", "--no-bin", $PythonVersion
        ) + $offline))) {
            Stop-Launcher "could not install managed Python $PythonVersion"
        }
        if (-not (Invoke-Uv $Uv (@(
            "venv", "--managed-python", "--python", $PythonVersion, $stage
        ) + $offline))) {
            Stop-Launcher "could not create application environment"
        }
        $python = Get-AppPython $stage
        Write-LauncherLog "installing locked sbk-analytics environment"
        $oldVenv = $env:VIRTUAL_ENV
        $env:VIRTUAL_ENV = $stage
        Push-Location $SourceRoot
        try {
            if (-not (Invoke-Uv $Uv (@(
                "sync", "--active", "--locked", "--no-editable", "--no-dev",
                "--reinstall-package", "sbk-analytics", "--python", $python
            ) + $offline))) {
                Stop-Launcher "could not install the locked application environment"
            }
        } finally {
            Pop-Location
            if ($null -eq $oldVenv) {
                Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
            } else { $env:VIRTUAL_ENV = $oldVenv }
        }
        Set-Content -LiteralPath (Join-Path $stage $BootstrapMarker) `
            -Value $Fingerprint -Encoding ASCII
        $metadata = [ordered]@{
            schema = 2; fingerprint = $Fingerprint; python = $PythonVersion
            platform = $PlatformId; uv = $UvVersion
        } | ConvertTo-Json -Compress
        Set-Content -LiteralPath (Join-Path $stage "metadata.json") `
            -Value $metadata -Encoding UTF8
        if (-not (Test-AppReady $stage $Fingerprint)) {
            Stop-Launcher "new application environment failed its health check"
        }
        Remove-Item -LiteralPath $EnvironmentRoot -Recurse -Force `
            -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $stage -Destination $EnvironmentRoot
    } finally {
        if (Test-Path -LiteralPath $stage) {
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        }
        Exit-BootstrapLock $lock
    }
}

Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
Remove-Item Env:CONDA_PREFIX -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
$Fingerprint = Get-SourceFingerprint
$AppEnvironment = Join-Path $AppRoot $Fingerprint
if (-not (Test-AppReady $AppEnvironment $Fingerprint)) {
    $Uv = Get-Uv
    Initialize-Application $Uv $Fingerprint $AppEnvironment
}

$Python = Get-AppPython $AppEnvironment
$env:VIRTUAL_ENV = $AppEnvironment
$env:SBK_ANALYTICS_SOURCE_ROOT = $SourceRoot
Remove-Item Env:CONDA_PREFIX -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
$env:PATH = "$(Join-Path $AppEnvironment 'Scripts');$env:PATH"
Write-LauncherLog "using managed application environment: $AppEnvironment"
& $Python -P -m analytics @CliArgs
exit $LASTEXITCODE
