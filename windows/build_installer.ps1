# Compile the Inno Setup installer (requires portable build first, and Inno Setup 6).
# Run from repo root on Windows:  powershell -File windows/build_installer.ps1
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
if (-not (Test-Path (Join-Path $Root "grokbot2api.py"))) {
    $Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
Set-Location $Root

$PortableDir = Join-Path $Root "windows\dist\grokbot2api"
if (-not (Test-Path (Join-Path $PortableDir "grokbot2api.exe"))) {
    Write-Host "Portable build missing; running build_portable.ps1 first..."
    & powershell -File (Join-Path $Root "windows\build_portable.ps1")
}

$Iscc = $null
foreach ($candidate in @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)) {
    if (Test-Path $candidate) { $Iscc = $candidate; break }
}
if (-not $Iscc) {
    throw "Inno Setup 6 (ISCC.exe) not found. Install from https://jrsoftware.org/isinfo.php"
}

$OutDir = Join-Path $Root "windows\dist"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
& $Iscc (Join-Path $Root "windows\installer.iss")
$Setup = Join-Path $OutDir "grokbot2api-windows-setup-x64.exe"
if (-not (Test-Path $Setup)) {
    throw "Installer output not found: $Setup"
}
Write-Host "Wrote $Setup"
