$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir '..\..')
$WorkerHome = if ($env:CORE_WORKER_HOME) { $env:CORE_WORKER_HOME } else { Join-Path $HOME 'core-worker' }
$EnvFile = if ($env:CORE_WORKER_ENV) { $env:CORE_WORKER_ENV } else { Join-Path $HOME '.core-worker.env' }

New-Item -ItemType Directory -Force -Path $WorkerHome, (Join-Path $WorkerHome 'secrets'), (Join-Path $WorkerHome 'cache'), (Join-Path $WorkerHome 'logs') | Out-Null
Copy-Item (Join-Path $RepoRoot 'deploy\termux\phone-worker\phone_worker.py') (Join-Path $WorkerHome 'phone_worker.py') -Force
Copy-Item (Join-Path $RepoRoot 'deploy\termux\phone-worker\music_agent.py') (Join-Path $WorkerHome 'music_agent.py') -Force
Copy-Item (Join-Path $ScriptDir 'start-core-worker.ps1') (Join-Path $WorkerHome 'start-core-worker.ps1') -Force

$Req = @'
aiohttp
discord.py[voice]>=2.7.1,<2.8
PyNaCl
davey
yt-dlp[default]
gTTS
edge-tts
psutil
google-cloud-texttospeech
requests>=2.31.0
'@
Set-Content -Path (Join-Path $WorkerHome 'requirements-worker.txt') -Value $Req -Encoding UTF8
py -3 -m venv (Join-Path $WorkerHome '.venv')
$Python = Join-Path $WorkerHome '.venv\Scripts\python.exe'
& $Python -m pip install -U pip wheel
& $Python -m pip install -r (Join-Path $WorkerHome 'requirements-worker.txt')

if (-not (Test-Path $EnvFile)) {
  Copy-Item (Join-Path $ScriptDir 'core-worker.env.example') $EnvFile
  (Get-Content $EnvFile) -replace 'CORE_WORKER_SOURCE=linux-pc-worker','CORE_WORKER_SOURCE=windows-pc-worker' -replace 'CORE_WORKER_DEVICE_TYPE=linux_pc','CORE_WORKER_DEVICE_TYPE=windows_pc' -replace 'CORE_WORKER_RUNTIME_MODE=linux-pc','CORE_WORKER_RUNTIME_MODE=windows-pc' | Set-Content $EnvFile
}

Write-Host "Instalado em $WorkerHome"
Write-Host "Edite $EnvFile e rode: powershell -ExecutionPolicy Bypass -File $WorkerHome\start-core-worker.ps1"
