[CmdletBinding()]
param([switch]$StopDocker)

$ErrorActionPreference = "Continue"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StatePath = Join-Path $ProjectRoot ".huai-coder-runtime\services.json"

function Stop-ProcessTree([int]$ProcessId) {
    if ($ProcessId -gt 0 -and (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & taskkill.exe /PID $ProcessId /T /F >$null 2>$null
        $success = ($LASTEXITCODE -eq 0) -or (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue))
        $ErrorActionPreference = $previousErrorActionPreference
        return $success
    }
    return $true
}

function Get-ListeningProcessIds([int]$Port) {
    $ids = @()
    $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($connections) {
        $ids += @($connections | Select-Object -ExpandProperty OwningProcess)
    } else {
        $lines = @(netstat.exe -ano -p tcp 2>$null | Select-String (":$Port\s+.*LISTENING\s+(\d+)$"))
        foreach ($line in $lines) {
            if ($line.Matches.Count -gt 0) { $ids += [int]$line.Matches[0].Groups[1].Value }
        }
    }
    return @($ids | Where-Object { $_ -gt 0 } | Select-Object -Unique)
}

if (Test-Path -LiteralPath $StatePath) {
    $state = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($service in @($state.runner, $state.playwright_mcp)) {
        if ($service.pid) { Stop-ProcessTree ([int]$service.pid) }
    }
    $state.runner.pid = 0
    $state.playwright_mcp.pid = 0
    $state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $StatePath -Encoding UTF8
}

foreach ($port in @(8765, 8931)) {
    foreach ($processId in @(Get-ListeningProcessIds $port)) {
        Stop-ProcessTree ([int]$processId)
    }
}

if ($StopDocker) {
    Push-Location $ProjectRoot
    try { docker compose stop } finally { Pop-Location }
}

Write-Host "Runner and Playwright MCP stopped."
