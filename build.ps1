<#
.SYNOPSIS
    Build LG600Studio.exe with PyInstaller.

.DESCRIPTION
    Produces a single windowed executable in `dist\LG600Studio.exe`. The build
    bundles Python, Tkinter, Pillow, imageio and its ffmpeg binary, so the result
    runs on a machine without Python installed.

    Usage:
        powershell -ExecutionPolicy Bypass -File build.ps1
        powershell -ExecutionPolicy Bypass -File build.ps1 -Clean

.PARAMETER Clean
    Remove `build\`, `dist\` and the generated spec file before building.
#>
[CmdletBinding()]
param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$python = $env:LG600_PYTHON
if (-not $python) {
    $candidates = @("C:\Python314\python.exe", "C:\Python313\python.exe", "C:\Python312\python.exe")
    $python = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $python) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    throw "No Python interpreter found. Install Python 3.10+ or set LG600_PYTHON."
}
Write-Host "Using Python: $python"

if ($Clean) {
    foreach ($path in @("build", "dist")) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
    }
    foreach ($path in @("LG600Studio.spec")) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
}

Write-Host "Checking dependencies..."
& $python -c "import PIL, tkinter; from PIL import ImageTk; print('Pillow + Tkinter OK')"
if ($LASTEXITCODE -ne 0) {
    throw "Pillow/Tkinter are missing. Run: $python -m pip install -r requirements.txt"
}

if (-not (Test-Path -LiteralPath "assets\app.ico")) {
    Write-Host "Generating icon..."
    & $python make_icon.py
    if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }
}

Write-Host "Building executable (this takes a minute)..."
$args = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "LG600Studio",
    "--icon", "assets\app.ico",
    "--add-data", "assets;assets",
    "--collect-all", "imageio_ffmpeg",
    "--collect-submodules", "lg6studio",
    "--hidden-import", "PIL._tkinter_finder",
    "launcher.py"
)
& $python @args
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$exe = Join-Path $PSScriptRoot "dist\LG600Studio.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "Build finished but $exe is missing."
}

$size = [math]::Round((Get-Item -LiteralPath $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "Built: $exe ($size MB)"
Write-Host "Run it with the TCOMAS software closed."
