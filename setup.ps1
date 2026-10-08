# phro-secretary prerequisites: Ollama + nomic-embed-text (local embeddings), Claude Code CLI.
# Each step skips what is already installed, so the script is safe to run again.
#   irm https://raw.githubusercontent.com/ThatsHoon/phro-secretary/main/setup.ps1 | iex
# ASCII only: Windows PowerShell 5.1 misreads a UTF-8 .ps1 without BOM.
& {
$ErrorActionPreference = 'Stop'
function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Refresh-Path { $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') }

Step 'Ollama + nomic-embed-text'
$ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if (-not ((Get-Command ollama -ErrorAction SilentlyContinue) -or (Test-Path $ollama))) {
  Invoke-RestMethod https://ollama.com/install.ps1 | Invoke-Expression
  Refresh-Path
}
if (Get-Command ollama -ErrorAction SilentlyContinue) { $ollama = (Get-Command ollama).Source }
function Ollama-Up { try { Invoke-RestMethod http://127.0.0.1:11434/api/version -TimeoutSec 2 | Out-Null; $true } catch { $false } }
$served = $null
if (-not (Ollama-Up)) {
  $served = Start-Process $ollama serve -WindowStyle Hidden -PassThru
  for ($i = 0; $i -lt 30 -and -not (Ollama-Up); $i++) { Start-Sleep 1 }
}
& $ollama pull nomic-embed-text
if ($LASTEXITCODE) { throw 'ollama pull failed' }
if ($served) { Stop-Process $served.Id }

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
