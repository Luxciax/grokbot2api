# Run on Windows after CopyFromBox places grokbot2api-desktop-sync.tgz somewhere local.
# Example:
#   CopyFromBox box_path=/workspace/grokbot2api-desktop-sync.tgz machineId=d44e1d3d-... computer_path=C:\Users\V\Downloads\grokbot2api-desktop-sync.tgz
#   powershell -File desktop\scripts\sync-from-box.ps1 -Archive C:\Users\V\Downloads\grokbot2api-desktop-sync.tgz

param(
  [Parameter(Mandatory = $true)][string]$Archive,
  [string]$RepoRoot = "C:\Users\V\grokbot2api"
)

if (-not (Test-Path $Archive)) { throw "Archive not found: $Archive" }
if (-not (Test-Path $RepoRoot)) { throw "Repo root not found: $RepoRoot" }

$tmp = Join-Path $env:TEMP ("grokbot2api-sync-" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  tar -xzf $Archive -C $tmp
  $map = @(
    @{ Src = "desktop"; Dst = "desktop" },
    @{ Src = ".github/workflows/windows-release.yml"; Dst = ".github\workflows\windows-release.yml" },
    @{ Src = "README.md"; Dst = "README.md" },
    @{ Src = ".gitignore"; Dst = ".gitignore" },
    @{ Src = "windows/README.md"; Dst = "windows\README.md" },
    @{ Src = "tests/test_os_crypt_v10.py"; Dst = "tests\test_os_crypt_v10.py" }
  )
  foreach ($m in $map) {
    $src = Join-Path $tmp $m.Src
    if (-not (Test-Path $src)) {
      Write-Warning "Missing in archive: $($m.Src)"
      continue
    }
    $dst = Join-Path $RepoRoot $m.Dst
    $parent = Split-Path $dst -Parent
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    if (Test-Path $src -PathType Container) {
      if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
      Copy-Item $src $dst -Recurse -Force
    } else {
      Copy-Item $src $dst -Force
    }
    Write-Host "Synced $($m.Src) -> $dst"
  }
  Write-Host "Done. Next: cd $RepoRoot\desktop; npm install; npm run tauri:dev"
}
finally {
  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
