Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $ProjectRoot 'backend'
$Desktop = Join-Path $ProjectRoot 'desktop'
$VirtualEnvironment = Join-Path $Backend '.venv'
$Python = Join-Path $VirtualEnvironment 'Scripts\python.exe'
$Pip = Join-Path $VirtualEnvironment 'Scripts\pip.exe'
$NodeModules = Join-Path $Desktop 'node_modules'
$EnvironmentFile = Join-Path $Backend '.env'
$EnvironmentExample = Join-Path $Backend '.env.example'

if (-not (Test-Path -LiteralPath $EnvironmentFile) -and (Test-Path -LiteralPath $EnvironmentExample)) {
  Copy-Item -LiteralPath $EnvironmentExample -Destination $EnvironmentFile
  Write-Host '[KoreAgent Next] Created backend\.env from .env.example. You may configure the model in the app or edit this file.'
}

function Invoke-CheckedCommand {
  param(
    [Parameter(Mandatory = $true)][string]$FilePath,
    [Parameter(Mandatory = $true)][string[]]$Arguments,
    [Parameter(Mandatory = $true)][string]$FailureMessage
  )

  & $FilePath @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "$FailureMessage (exit code: $LASTEXITCODE)"
  }
}

$PyLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
if (-not $PyLauncher) {
  throw 'Python launcher (py.exe) was not found. Install Python 3.12 and enable the Python launcher.'
}

if (-not (Test-Path -LiteralPath $Python) -or -not (Test-Path -LiteralPath $Pip)) {
  if (Test-Path -LiteralPath $VirtualEnvironment) {
    Write-Host '[KoreAgent Next] Removing the incomplete Python environment...'
    try {
      Remove-Item -LiteralPath $VirtualEnvironment -Recurse -Force
    } catch {
      throw "Cannot remove the incomplete Python environment. Close programs using this project and run this script again. Details: $($_.Exception.Message)"
    }
  }

  Write-Host '[KoreAgent Next] Creating the Python 3.12 environment...'
  Invoke-CheckedCommand -FilePath $PyLauncher.Source `
    -Arguments @('-3.12', '-m', 'venv', $VirtualEnvironment) `
    -FailureMessage 'Failed to create the Python virtual environment.'

  if (-not (Test-Path -LiteralPath $Python) -or -not (Test-Path -LiteralPath $Pip)) {
    throw 'Python reported success, but the virtual environment is incomplete. Repair or reinstall Python 3.12 and try again.'
  }
}

& $Python -c 'import fastapi, uvicorn' 2>$null
$BackendDependenciesReady = $LASTEXITCODE -eq 0
if (-not $BackendDependenciesReady) {
  Write-Host '[KoreAgent Next] Installing backend dependencies...'
  Invoke-CheckedCommand -FilePath $Python `
    -Arguments @('-m', 'pip', 'install', '-r', (Join-Path $Backend 'requirements.txt')) `
    -FailureMessage 'Failed to install backend dependencies.'
}

$NpmCommand = Get-Command 'npm.cmd' -ErrorAction SilentlyContinue
if (-not $NpmCommand) {
  throw 'npm.cmd was not found. Install Node.js 20 or newer and reopen PowerShell.'
}

$RequiredDesktopFiles = @(
  (Join-Path $NodeModules '.bin\concurrently.cmd'),
  (Join-Path $NodeModules '.bin\vite.cmd'),
  (Join-Path $NodeModules 'electron\dist\electron.exe')
)
$DesktopDependenciesReady = $true
foreach ($RequiredFile in $RequiredDesktopFiles) {
  if (-not (Test-Path -LiteralPath $RequiredFile)) {
    $DesktopDependenciesReady = $false
    break
  }
}

if (-not $DesktopDependenciesReady) {
  Write-Host '[KoreAgent Next] Desktop dependencies are missing or incomplete.'
  if (Test-Path -LiteralPath $NodeModules) {
    Write-Host '[KoreAgent Next] Removing the incomplete node_modules directory...'
    try {
      Remove-Item -LiteralPath $NodeModules -Recurse -Force
    } catch {
      throw "Cannot remove the incomplete node_modules directory. Close Electron, Node.js, editors, and terminals using this project, then run this script again. Details: $($_.Exception.Message)"
    }
  }

  $PreviousElectronMirror = $env:ELECTRON_MIRROR
  if ([string]::IsNullOrWhiteSpace($PreviousElectronMirror)) {
    $env:ELECTRON_MIRROR = 'https://npmmirror.com/mirrors/electron/'
    Write-Host '[KoreAgent Next] Using the Electron download mirror for this installation.'
  }

  try {
    Invoke-CheckedCommand -FilePath $NpmCommand.Source `
      -Arguments @('--prefix', $Desktop, 'ci', '--no-audit', '--no-fund') `
      -FailureMessage 'Failed to install desktop dependencies. Check the network connection and run the script again.'
  } finally {
    if ([string]::IsNullOrWhiteSpace($PreviousElectronMirror)) {
      Remove-Item Env:\ELECTRON_MIRROR -ErrorAction SilentlyContinue
    } else {
      $env:ELECTRON_MIRROR = $PreviousElectronMirror
    }
  }

  foreach ($RequiredFile in $RequiredDesktopFiles) {
    if (-not (Test-Path -LiteralPath $RequiredFile)) {
      throw "Desktop dependency installation completed but a required file is still missing: $RequiredFile"
    }
  }
}

Write-Host '[KoreAgent Next] Starting backend and desktop application...'
$HostProcess = Start-Process -FilePath $Python `
  -ArgumentList '-m', 'kore_next.server' `
  -WorkingDirectory $Backend `
  -WindowStyle Hidden `
  -PassThru

try {
  Invoke-CheckedCommand -FilePath $NpmCommand.Source `
    -Arguments @('--prefix', $Desktop, 'run', 'electron:dev') `
    -FailureMessage 'The desktop application exited with an error.'
} finally {
  if (-not $HostProcess.HasExited) {
    Stop-Process -Id $HostProcess.Id -ErrorAction SilentlyContinue
  }
}
