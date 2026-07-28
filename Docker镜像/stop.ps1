[CmdletBinding()]
param([switch]$RemoveData)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $root '.env'
$compose = Join-Path $root 'docker-compose.yml'

$arguments = @('compose', '--env-file', $envFile, '--file', $compose, 'down')
if ($RemoveData) {
    $arguments += '--volumes'
}

Push-Location $root
try {
    & docker @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose 停止失败，退出码：$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
