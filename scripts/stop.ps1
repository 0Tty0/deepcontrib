$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $repoRoot ".deepcontrib"
$pidPath = Join-Path $runtimeDir "pids.json"

function Stop-ProcessTree([int]$Id) {
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $Id" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree ([int]$child.ProcessId)
    }
    if (Get-Process -Id $Id -ErrorAction SilentlyContinue) {
        Stop-Process -Id $Id -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host "No DeepContrib process record was found."
    exit 0
}

$processes = Get-Content -Raw -LiteralPath $pidPath | ConvertFrom-Json
foreach ($name in @("backend", "frontend")) {
    $id = [int]$processes.$name
    $process = Get-Process -Id $id -ErrorAction SilentlyContinue
    if ($null -ne $process) {
        Stop-ProcessTree $id
        Write-Host "Stopped $name ($id)."
    }
}
Remove-Item -LiteralPath $pidPath -Force
Write-Host "PostgreSQL was left running. Use 'docker compose down' when its data is no longer needed."
