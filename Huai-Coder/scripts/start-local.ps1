[CmdletBinding()]
param(
    [string]$Workspace,
    [switch]$SkipDocker,
    [switch]$Headless
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendRoot = Join-Path $ProjectRoot "backend"
$RuntimeRoot = Join-Path $ProjectRoot ".huai-coder-runtime"
$LogRoot = Join-Path $RuntimeRoot "logs"
$NpmCache = Join-Path $RuntimeRoot "npm-cache"
$StatePath = Join-Path $RuntimeRoot "services.json"

function Get-ExecutablePath([string[]]$Names) {
    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command -and $command.Source) {
            return $command.Source
        }
    }
    return $null
}

function Require-Executable([string[]]$Names, [string]$Label) {
    $path = Get-ExecutablePath $Names
    if (-not $path) {
        throw "$Label was not found in PATH."
    }
    return $path
}

function Find-Python {
    $patterns = @(
        (Join-Path $Workspace ".huai-coder-venv\Scripts\python.exe"),
        (Join-Path $Workspace ".venv\Scripts\python.exe"),
        (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Python\pythoncore-*\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python*\python.exe"),
        (Join-Path $env:ProgramFiles "Python*\python.exe")
    )
    foreach ($pattern in $patterns) {
        $candidate = @(Resolve-Path -Path $pattern -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($candidate) { return $candidate.Path }
    }

    return Get-ExecutablePath @("python.exe", "python3.exe", "py.exe")
}

function Normalize-ProcessEnvironment {
    $pathNames = @([Environment]::GetEnvironmentVariables("Process").Keys | Where-Object { $_ -ieq "PATH" })
    if ($pathNames.Count -gt 1) {
        $pathValue = $env:Path
        [Environment]::SetEnvironmentVariable("Path", $null, "Process")
        [Environment]::SetEnvironmentVariable("PATH", $null, "Process")
        [Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")
    }
}

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

function Stop-PreviousServices {
    if (-not (Test-Path -LiteralPath $StatePath)) { return }
    try {
        $previous = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($service in @($previous.runner, $previous.playwright_mcp)) {
            if ($service.pid) { Stop-ProcessTree ([int]$service.pid) }
        }
    } catch {
        Write-Warning "Unable to read previous service state: $($_.Exception.Message)"
    }
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

function Stop-PortOwners([int]$Port) {
    foreach ($processId in @(Get-ListeningProcessIds $Port)) {
        Write-Host "Stopping old Huai-Coder service on port $Port (PID $processId)"
        if (-not (Stop-ProcessTree ([int]$processId))) {
            throw "Cannot stop the process on port $Port (PID $processId). Close the old service or run this script as Administrator."
        }
    }
}

function Ensure-McpConfig {
    $configPath = Join-Path $BackendRoot "mcp.json"
    $examplePath = Join-Path $BackendRoot "mcp.example.json"
    if (-not (Test-Path -LiteralPath $configPath)) {
        Copy-Item -LiteralPath $examplePath -Destination $configPath
    }

    $document = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $document.mcpServers) {
        $document | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
    }
    $server = $document.mcpServers.'playwright-host'
    if (-not $server) {
        $server = [pscustomobject]@{}
        $document.mcpServers | Add-Member -NotePropertyName 'playwright-host' -NotePropertyValue $server
    }

    $server.enabled = $true
    $server.transport = "sse"
    $server.url = "http://host.docker.internal:8931/sse"
    $server.scope = "user"
    $server.allowedTools = @(
        "browser_navigate",
        "browser_tabs",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_wait_for"
    )
    $server.approval = [pscustomobject]@{
        browser_click = "auto"
        browser_type = "auto"
        browser_wait_for = "auto"
    }
    $json = $document | ConvertTo-Json -Depth 20
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($configPath, $json, $utf8NoBom)
    return $configPath
}

function Start-HiddenService {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )
    $stdout = Join-Path $LogRoot "$Name.stdout.log"
    $stderr = Join-Path $LogRoot "$Name.stderr.log"
    Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    return [pscustomobject]@{ name = $Name; pid = $process.Id; stdout = $stdout; stderr = $stderr }
}

function Wait-HttpEndpoint([string]$Uri, [int[]]$AcceptedStatusCodes = @(200), [int]$TimeoutSeconds = 45) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Uri -Method Get -TimeoutSec 3 -UseBasicParsing
            if ($AcceptedStatusCodes -contains [int]$response.StatusCode) { return $true }
        } catch {
            $response = $_.Exception.Response
            if ($response -and ($AcceptedStatusCodes -contains [int]$response.StatusCode.value__)) { return $true }
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    return $false
}

if (-not $Workspace) {
    $Workspace = Read-Host "Enter the absolute workspace path"
}
if (-not (Test-Path -LiteralPath $Workspace -PathType Container)) {
    throw "Workspace does not exist: $Workspace"
}
$Workspace = (Resolve-Path -LiteralPath $Workspace).Path

New-Item -ItemType Directory -Path $RuntimeRoot, $LogRoot, $NpmCache -Force | Out-Null
Stop-PreviousServices
Stop-PortOwners 8765
Stop-PortOwners 8931
$configPath = Ensure-McpConfig
$pythonPath = Find-Python
if (-not $pythonPath) { throw "Python was not found. Install Python or add it to PATH." }
$npxPath = Require-Executable @("npx.cmd", "npx.exe") "npx"
$powershellPath = Require-Executable @("powershell.exe", "pwsh.exe") "PowerShell"
Normalize-ProcessEnvironment

Write-Host "[1/4] Starting local Runner for $Workspace"
$quotedWorkspace = '"' + $Workspace + '"'
if ([System.IO.Path]::GetFileName($pythonPath).ToLowerInvariant() -eq "py.exe") {
    $runnerArguments = @("-3", "-m", "app.runner_server", "--workspace", $quotedWorkspace, "--host", "127.0.0.1", "--port", "8765")
} else {
    $runnerArguments = @("-m", "app.runner_server", "--workspace", $quotedWorkspace, "--host", "127.0.0.1", "--port", "8765")
}
$runner = Start-HiddenService "runner" $pythonPath $runnerArguments $BackendRoot

Write-Host "[2/4] Starting Playwright MCP"
$escapedCache = $NpmCache.Replace("'", "''")
$escapedNpx = $npxPath.Replace("'", "''")
$mcpCommand = "`$env:npm_config_cache='$escapedCache'; & '$escapedNpx' -y '@playwright/mcp@latest' --port 8931 --host 0.0.0.0 --allowed-hosts '*' --isolated"
if ($Headless) { $mcpCommand += " --headless" }
$mcp = Start-HiddenService "playwright-mcp" $powershellPath @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $mcpCommand) $ProjectRoot

if (-not (Wait-HttpEndpoint "http://127.0.0.1:8765/health" @(200) 45)) {
    throw "Runner failed to start. Check $($runner.stderr)"
}
if (-not (Wait-HttpEndpoint "http://127.0.0.1:8931/mcp" @(200, 400, 405) 60)) {
    throw "Playwright MCP failed to start. Check $($mcp.stderr)"
}

if (-not $SkipDocker) {
    Write-Host "[3/4] Starting and recreating Docker services"
    Push-Location $ProjectRoot
    try {
        & docker compose up -d --build --force-recreate
        if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed to start." }
    } finally {
        Pop-Location
    }
    if (-not (Wait-HttpEndpoint "http://127.0.0.1:8000/health" @(200) 90)) {
        throw "Docker backend health check failed."
    }
} else {
    Write-Host "[3/4] Docker startup skipped (-SkipDocker)"
}

if (-not $SkipDocker) {
    Write-Host "[4/4] Refreshing MCP tools"
    try {
        $refresh = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/mcp/refresh" -Method Post -TimeoutSec 30
        $ready = @($refresh.servers | Where-Object { $_.id -eq "playwright-host" -and $_.status -eq "ready" })
        if ($ready.Count -eq 0) {
            throw "playwright-host is not ready."
        }
        Write-Host "Browser MCP connected. Found $($ready[0].tool_count) tools."
    } catch {
        Write-Warning "MCP refresh failed: $($_.Exception.Message)"
        Write-Warning "Check that Docker can reach host.docker.internal:8931, then click MCP Refresh in the UI."
    }
} else {
    Write-Host "[4/4] MCP refresh skipped because Docker was skipped"
}

@{
    started_at = (Get-Date).ToString("o")
    workspace = $Workspace
    config = $configPath
    runner = $runner
    playwright_mcp = $mcp
} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $StatePath -Encoding UTF8

Write-Host ""
Write-Host "Startup complete."
Write-Host "  Web:      http://localhost"
Write-Host "  Runner:   http://127.0.0.1:8765/health"
Write-Host "  MCP:      http://127.0.0.1:8931/mcp"
Write-Host "  Workspace: $Workspace"
Write-Host "Stop services with: scripts\stop-local.cmd"
