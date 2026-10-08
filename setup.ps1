# phro-secretary prerequisite: Claude Code CLI (the app ships everything else).
# Skips what is already installed, so the script is safe to run again.
#   irm https://raw.githubusercontent.com/ThatsHoon/phro-secretary/main/setup.ps1 | iex
# ASCII only: Windows PowerShell 5.1 misreads a UTF-8 .ps1 without BOM.
& {
$ErrorActionPreference = 'Stop'
function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Refresh-Path { $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') }

Step 'Claude Code CLI'
$bin = "$HOME\.local\bin"
if (-not ((Get-Command claude -ErrorAction SilentlyContinue) -or (Test-Path "$bin\claude.exe"))) {
  Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
}
# The app finds claude through PATH, so make sure the native install directory is on it.
$userPath = [Environment]::GetEnvironmentVariable('Path','User')
if ((Test-Path "$bin\claude.exe") -and ($userPath -split ';') -notcontains $bin) {
  [Environment]::SetEnvironmentVariable('Path', "$userPath;$bin", 'User')
}
Refresh-Path

Write-Host "`nDone. Last step: run 'claude' once in a new terminal and log in, then install and start phro-secretary." -ForegroundColor Green
}
