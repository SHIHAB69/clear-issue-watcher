# Watcher installer (Windows PowerShell). Installs the `watcher` command.
#   irm <url>/install.ps1 | iex
$ErrorActionPreference = "Stop"
$Repo = $env:WATCHER_REPO; if (-not $Repo) { $Repo = "https://github.com/SHIHAB69/clear-issue-watcher.git" }
$Dest = $env:WATCHER_DIR;  if (-not $Dest) { $Dest = "$HOME\tools\clear-issue-watcher" }

Write-Host "> Watcher installer"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "python required" }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git required" }

if (Test-Path "$Dest\.git") {
  Write-Host "> Updating $Dest"; git -C $Dest pull --ff-only
} else {
  Write-Host "> Cloning into $Dest"; New-Item -ItemType Directory -Force -Path (Split-Path $Dest) | Out-Null; git clone $Repo $Dest
}

Write-Host "> Installing the watcher command"
python -m pip install --user -e $Dest

Write-Host ""
Write-Host "Installed. If 'watcher' isn't found, add your Python user Scripts dir to PATH."
Write-Host "Next:"
Write-Host "  watcher doctor"
Write-Host "  cd <your-project>; watcher     # add a source"
Write-Host "  watcher start"
