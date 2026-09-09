param(
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
$runtimeDir = Join-Path $repoRoot ".deepcontrib"
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

function Import-DotEnv([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*(?<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)\s*$') {
            $value = $Matches.value.Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            [Environment]::SetEnvironmentVariable($Matches.name, $value, "Process")
        }
    }
}

function Stop-ProcessTree([int]$Id) {
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $Id" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree ([int]$child.ProcessId)
    }
    if (Get-Process -Id $Id -ErrorAction SilentlyContinue) {
        Stop-Process -Id $Id -Force -ErrorAction SilentlyContinue
    }
}

Import-DotEnv (Join-Path $repoRoot ".env")
if (-not $SkipChecks) {
    & (Join-Path $PSScriptRoot "check.ps1") -SkipDatabase
}

docker compose up -d postgres
if ($LASTEXITCODE -ne 0) { throw "Could not start PostgreSQL." }

$deadline = (Get-Date).AddSeconds(60)
do {
    $postgresContainer = docker compose ps -q postgres 2>$null
    $health = if ($postgresContainer) {
        docker inspect --format '{{.State.Health.Status}}' $postgresContainer 2>$null
    } else {
        "missing"
    }
    if ($health -eq "healthy") { break }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
if ($health -ne "healthy") { throw "PostgreSQL did not become healthy within 60 seconds." }

$backendOut = Join-Path $runtimeDir "backend.log"
$backendErr = Join-Path $runtimeDir "backend.error.log"
$frontendOut = Join-Path $runtimeDir "frontend.log"
$frontendErr = Join-Path $runtimeDir "frontend.error.log"
$backend = Start-Process -FilePath "uv" -ArgumentList @(
    "run", "--project", "backend", "--python", "3.11", "--no-editable",
    "--reinstall-package", "deepcontrib-backend",
    "deepcontrib", "serve", "--host", "127.0.0.1", "--port", "8000"
) -WorkingDirectory $repoRoot -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr -WindowStyle Hidden -PassThru
$nextCli = Join-Path $repoRoot "frontend\node_modules\next\dist\bin\next"
$frontend = Start-Process -FilePath "node.exe" -ArgumentList @(
    $nextCli, "dev", "--hostname", "127.0.0.1"
) -WorkingDirectory (Join-Path $repoRoot "frontend") -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr -WindowStyle Hidden -PassThru

@{ backend = $backend.Id; frontend = $frontend.Id } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimeDir "pids.json") -Encoding UTF8

function Wait-Http([string]$Url, [string]$Name) {
    $deadline = (Get-Date).AddSeconds(60)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $Url
            if ($response.StatusCode -eq 200) { return }
        } catch {
            # The child process may still be initializing.
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "$Name did not become ready within 60 seconds. See $runtimeDir logs."
}

try {
    Wait-Http "http://127.0.0.1:8000/api/v1/health" "DeepContrib API"
    Wait-Http "http://127.0.0.1:3000/" "DeepContrib workbench"
} catch {
    foreach ($process in @($backend, $frontend)) {
        if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
            Stop-ProcessTree $process.Id
        }
    }
    Remove-Item -LiteralPath (Join-Path $runtimeDir "pids.json") -Force -ErrorAction SilentlyContinue
    throw
}

Write-Host "DeepContrib started: API http://127.0.0.1:8000, UI http://127.0.0.1:3000"
Write-Host "Logs: $runtimeDir"
