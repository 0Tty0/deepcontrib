param(
    [switch]$SkipDatabase,
    [switch]$SkipModel
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found on PATH."
    }
}

foreach ($command in @("uv", "node", "npm", "docker", "gh")) {
    Require-Command $command
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is not running with a Linux engine."
}

$envPath = Join-Path $repoRoot ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    Write-Warning "No .env file found. Copy .env.example and configure the model before creating tasks."
}

if (-not $SkipModel) {
    $model = [Environment]::GetEnvironmentVariable("DEEPCONTRIB_MODEL", "Process")
    if ([string]::IsNullOrWhiteSpace($model)) {
        throw "DEEPCONTRIB_MODEL is not set in this PowerShell process."
    }
    $provider = ($model -split ":", 2)[0]
    $keyByProvider = @{
        openai = "OPENAI_API_KEY"
        anthropic = "ANTHROPIC_API_KEY"
        google_genai = "GOOGLE_API_KEY"
        openrouter = "OPENROUTER_API_KEY"
        fireworks = "FIREWORKS_API_KEY"
        baseten = "BASETEN_API_KEY"
    }
    if ($keyByProvider.ContainsKey($provider)) {
        $keyName = $keyByProvider[$provider]
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($keyName, "Process"))) {
            throw "$keyName is not set in this PowerShell process."
        }
    }
}

if (-not $SkipDatabase) {
    $composeStatus = docker compose ps --status running --services
    if ($LASTEXITCODE -ne 0 -or $composeStatus -notcontains "postgres") {
        throw "The PostgreSQL compose service is not running. Run scripts/start.ps1 first."
    }
}

Write-Host "DeepContrib prerequisites are ready."
