$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
$tag = "deepcontrib-python-pytest:8.4.2"
docker build --file backend/docker/test-image/Dockerfile --tag $tag .
if ($LASTEXITCODE -ne 0) { throw "Could not build the application-owned test image." }
$digest = docker image inspect $tag --format '{{.Id}}'
Write-Host "Built $tag with local image id $digest"
Write-Host "Set DEEPCONTRIB_TEST_IMAGE to a registry digest before using the test runner."
