$ErrorActionPreference = 'Stop'
$WorkerHome = if ($env:CORE_WORKER_HOME) { $env:CORE_WORKER_HOME } else { Join-Path $HOME 'core-worker' }
$EnvFile = if ($env:CORE_WORKER_ENV) { $env:CORE_WORKER_ENV } else { Join-Path $HOME '.core-worker.env' }

if (Test-Path $EnvFile) {
  Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
      $parts = $line.Split('=', 2)
      [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim().Trim('"').Trim("'"), 'Process')
    }
  }
}

$env:CORE_WORKER_SOURCE = if ($env:CORE_WORKER_SOURCE) { $env:CORE_WORKER_SOURCE } else { 'windows-pc-worker' }
$env:CORE_WORKER_DEVICE_TYPE = if ($env:CORE_WORKER_DEVICE_TYPE) { $env:CORE_WORKER_DEVICE_TYPE } else { 'windows_pc' }
$env:CORE_WORKER_RUNTIME_MODE = if ($env:CORE_WORKER_RUNTIME_MODE) { $env:CORE_WORKER_RUNTIME_MODE } else { 'windows-pc' }
$env:PHONE_WORKER_ENV = $EnvFile
$env:PHONE_WORKER_DIR = $WorkerHome
$env:PHONE_WORKER_HOST = if ($env:CORE_WORKER_HOST) { $env:CORE_WORKER_HOST } else { '127.0.0.1' }
$env:PHONE_WORKER_PORT = if ($env:CORE_WORKER_PORT) { $env:CORE_WORKER_PORT } else { '8766' }

New-Item -ItemType Directory -Force -Path $WorkerHome, (Join-Path $WorkerHome 'secrets'), (Join-Path $WorkerHome 'cache'), (Join-Path $WorkerHome 'logs') | Out-Null
$Python = Join-Path $WorkerHome '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
  py -3 -m venv (Join-Path $WorkerHome '.venv')
}
Set-Location $WorkerHome
$Token = if ($env:PHONE_WORKER_TOKEN) { $env:PHONE_WORKER_TOKEN } else { $env:CORE_WORKER_TOKEN }
& $Python (Join-Path $WorkerHome 'phone_worker.py') --host $env:PHONE_WORKER_HOST --port $env:PHONE_WORKER_PORT --token $Token
