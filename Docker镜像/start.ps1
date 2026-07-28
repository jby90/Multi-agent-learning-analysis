[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $root '.env'
$example = Join-Path $root '.env.example'
$compose = Join-Path $root 'docker-compose.yml'

if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    throw '请先复制 .env.example 为 .env，并填写两个数据库密码和模型 API Key。'
}

$text = Get-Content -LiteralPath $envFile -Encoding utf8 -Raw
if ($text -match 'replace_with_') {
    throw '.env 仍含占位值，请完成配置后再启动。'
}

$allowedSettings = @(
    'MYSQL_ROOT_PASSWORD'
    'REF_READER_PASSWORD'
    'DASHSCOPE_API_KEY'
    'MYSQL_HOST_PORT'
    'BACKEND_HOST_PORT'
    'FRONTEND_HOST_PORT'
)
$settings = @{}
Get-Content -LiteralPath $envFile -Encoding utf8 | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
        $name = $matches[1].Trim()
        if ($settings.ContainsKey($name)) {
            throw "配置项重复：$name"
        }
        $settings[$name] = $matches[2]
    }
}
foreach ($name in $allowedSettings) {
    if (-not $settings.ContainsKey($name) -or -not [string]$settings[$name]) {
        throw "缺少配置项：$name"
    }
    [Environment]::SetEnvironmentVariable(
        $name,
        [string]$settings[$name],
        [EnvironmentVariableTarget]::Process
    )
}

Push-Location $root
try {
    & docker compose --env-file $envFile --file $compose up --detach --wait --wait-timeout 300
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose 启动失败，退出码：$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$port = if ($settings['FRONTEND_HOST_PORT']) { $settings['FRONTEND_HOST_PORT'] } else { '18080' }
Write-Host "系统已启动：http://127.0.0.1:$port"
