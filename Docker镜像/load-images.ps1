[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$archives = @(
    'multiagent-decision-database_1.1.0.tar'
    'multiagent-decision-backend_1.1.0.tar'
    'multiagent-decision-frontend_1.1.0.tar'
)

foreach ($archive in $archives) {
    $path = Join-Path $root $archive
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "镜像文件不存在：$archive"
    }
    & docker image load --input $path
    if ($LASTEXITCODE -ne 0) {
        throw "镜像导入失败：$archive"
    }
}

Write-Host '三份镜像已导入。'
