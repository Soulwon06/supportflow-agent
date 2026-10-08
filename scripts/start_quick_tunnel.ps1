$ErrorActionPreference = "Stop"

$origin = "http://127.0.0.1:8001"
$healthUrl = "$origin/health"
$cloudflared = Get-Command cloudflared -ErrorAction SilentlyContinue
$localCloudflared = Join-Path $PSScriptRoot "..\.local\cloudflared.exe"

if ($cloudflared) {
    $cloudflaredPath = $cloudflared.Source
} elseif (Test-Path $localCloudflared) {
    $cloudflaredPath = (Resolve-Path $localCloudflared).Path
} else {
    throw "cloudflared is not installed. Install the official Cloudflare client first, then run this script again."
}

try {
    $health = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 8
    if ($health.StatusCode -ne 200) {
        throw "SupportFlow health check returned HTTP $($health.StatusCode)."
    }
} catch {
    throw "SupportFlow is not reachable at $origin. Start the local server first. Details: $($_.Exception.Message)"
}

Write-Host "SupportFlow is healthy at $origin" -ForegroundColor Green
Write-Host "Starting a temporary HTTPS URL. Keep this window and the SupportFlow server running." -ForegroundColor Yellow
& $cloudflaredPath tunnel --url $origin
