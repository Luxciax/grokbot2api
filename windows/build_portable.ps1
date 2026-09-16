# Build the portable onedir folder and zip it.
# Run from repo root on Windows:  powershell -File windows/build_portable.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root "grokbot2api.py"))) {
    $Root = $PSScriptRoot + "\.."
}
$Root = (Resolve-Path $Root).Path
Set-Location $Root

$Version = "0.2.0"
if (Test-Path (Join-Path $Root "grokbot2api.py")) {
    $m = Select-String -Path (Join-Path $Root "grokbot2api.py") -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { $Version = $m.Matches[0].Groups[1].Value }
}

Write-Host "Building portable grokbot2api $Version ..."
python -m pip install -r windows/requirements-build.txt
if (Test-Path "windows\dist") { Remove-Item -Recurse -Force "windows\dist" }
if (Test-Path "windows\build") { Remove-Item -Recurse -Force "windows\build" }
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }

python -m PyInstaller --noconfirm --clean --distpath windows/dist --workpath windows/build windows/grokbot2api.spec

$OutDir = Join-Path $Root "windows\dist\grokbot2api"
if (-not (Test-Path (Join-Path $OutDir "grokbot2api.exe"))) {
    throw "Portable build failed: grokbot2api.exe not found in $OutDir"
}

Copy-Item (Join-Path $Root "windows\README.md") (Join-Path $OutDir "README.md") -Force
$ZipName = "grokbot2api-windows-portable-x64.zip"
$ZipPath = Join-Path $Root "windows\dist\$ZipName"
if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
Compress-Archive -Path (Join-Path $OutDir "*") -DestinationPath $ZipPath -Force
Write-Host "Wrote $ZipPath"
