# phro-secretary prerequisites: WSL Ubuntu-24.04 with FalkorDB (phro-falkor service), Ollama + nomic-embed-text,
# Claude Code CLI. Each step skips what is already installed, so the script is safe to run again.
#   irm https://raw.githubusercontent.com/ThatsHoon/phro-secretary/main/setup.ps1 | iex
# ASCII only: Windows PowerShell 5.1 misreads a UTF-8 .ps1 without BOM.
& {
$ErrorActionPreference = 'Stop'
$Distro = 'Ubuntu-24.04'
$env:WSL_UTF8 = '1'  # wsl.exe -l prints UTF-16 otherwise
function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Refresh-Path { $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') }
function Wsl-Root($script) {
  # base64 keeps CRLF and PowerShell's stdin encoding out of the bash script
  $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($script -replace "`r", '')))
  wsl.exe -d $Distro -u root -- bash -c "echo $b64 | base64 -d | bash"
  if ($LASTEXITCODE) { throw "WSL step failed ($LASTEXITCODE)" }
}

Step "WSL $Distro"
if (((wsl.exe -l -q) -join "`n") -notmatch [regex]::Escape($Distro)) {
  wsl.exe --install -d $Distro
  Write-Host "Create your Ubuntu user in the window that opened (reboot if Windows asks), then run this script again." -ForegroundColor Yellow
  return
}
if ((wsl.exe -d $Distro -u root -- ps -p 1 -o comm=) -notmatch 'systemd') {
  Wsl-Root "printf '\n[boot]\nsystemd=true\n' >> /etc/wsl.conf"
  wsl.exe --terminate $Distro
}

Step 'FalkorDB (phro-falkor service, 127.0.0.1:6379)'
Wsl-Root @'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
FALKOR_URL=https://github.com/FalkorDB/FalkorDB/releases/download/v4.20.7/falkordb-x64.so
FALKOR_SHA=0e610855b4ffe21a7f8f182ac7ad2d23ee9b439366e97f990e7b34c2330d4410
if ! command -v redis-server >/dev/null; then
  apt-get update -q
  apt-get install -yq curl gpg lsb-release
  curl -fsSL https://packages.redis.io/gpg | gpg --dearmor --yes -o /usr/share/keyrings/redis-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb $(lsb_release -cs) main" > /etc/apt/sources.list.d/redis.list
  apt-get update -q
  apt-get install -yq redis-server libgomp1
fi
systemctl disable --now redis-server 2>/dev/null || true  # phro-falkor owns 6379
if [ "$(sha256sum /opt/falkordb/falkordb.so 2>/dev/null | cut -d' ' -f1)" != "$FALKOR_SHA" ]; then
  curl -fsSL -o /tmp/falkordb.so "$FALKOR_URL"
  echo "$FALKOR_SHA  /tmp/falkordb.so" | sha256sum -c -
  install -D -m 755 /tmp/falkordb.so /opt/falkordb/falkordb.so
  rm /tmp/falkordb.so
fi
install -d -o redis -g redis /var/lib/phro-falkor
cat > /etc/systemd/system/phro-falkor.service <<'EOF'
[Unit]
Description=Local FalkorDB for phro-graph
After=network.target

[Service]
Type=simple
User=redis
Group=redis
ExecStart=/usr/bin/redis-server --bind 127.0.0.1 --port 6379 --protected-mode yes --dir /var/lib/phro-falkor --appendonly yes --appendfsync everysec --save 60 1 --loadmodule /opt/falkordb/falkordb.so --daemonize no
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now phro-falkor
for i in $(seq 20); do redis-cli PING >/dev/null 2>&1 && break; sleep 0.5; done
redis-cli MODULE LIST | grep -x graph >/dev/null
echo 'FalkorDB ready'
'@

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
