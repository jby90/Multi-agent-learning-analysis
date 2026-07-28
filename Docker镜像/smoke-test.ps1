[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $root '.env'
$compose = Join-Path $root 'docker-compose.yml'

if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    throw '缺少 .env，请先按 README 完成配置并启动服务。'
}

$settings = @{}
Get-Content -LiteralPath $envFile -Encoding utf8 | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
        $settings[$matches[1].Trim()] = $matches[2]
    }
}
foreach ($name in @(
    'MYSQL_ROOT_PASSWORD',
    'REF_READER_PASSWORD',
    'DASHSCOPE_API_KEY',
    'MYSQL_HOST_PORT',
    'BACKEND_HOST_PORT',
    'FRONTEND_HOST_PORT'
)) {
    if (-not $settings.ContainsKey($name) -or -not [string]$settings[$name]) {
        throw "缺少配置项：$name"
    }
    [Environment]::SetEnvironmentVariable(
        $name,
        [string]$settings[$name],
        [EnvironmentVariableTarget]::Process
    )
}

$frontendPort = $settings['FRONTEND_HOST_PORT']
$baseUrl = "http://127.0.0.1:$frontendPort"
$index = Invoke-WebRequest -Uri "$baseUrl/" -UseBasicParsing
if ($index.StatusCode -ne 200 -or $index.Content -notmatch '<div id="app">') {
    throw '前端首页检查失败。'
}

$session = Invoke-RestMethod `
    -Uri "$baseUrl/api/sessions" `
    -Method Post `
    -ContentType 'application/json' `
    -Body '{"profile_id":"planner_new"}'
if (-not $session.session_id -or $session.awaiting -ne 'pretest') {
    throw '交互会话创建失败。'
}

$pretest = Invoke-RestMethod `
    -Uri "$baseUrl/api/sessions/$($session.session_id)/pretest" `
    -Method Get
if ($pretest.questions.Count -ne 5) {
    throw '岗前测评题数量异常。'
}

$sql = @'
SELECT 'dim_date', COUNT(*) FROM ref_contest_db.dim_date;
SELECT 'dim_process', COUNT(*) FROM ref_contest_db.dim_process;
SELECT 'dim_ship', COUNT(*) FROM ref_contest_db.dim_ship;
SELECT 'dim_workshop', COUNT(*) FROM ref_contest_db.dim_workshop;
SELECT 'fact_production_progress', COUNT(*) FROM ref_contest_db.fact_production_progress;
SHOW GRANTS FOR CURRENT_USER;
'@
$output = $sql | & docker compose `
    --env-file $envFile `
    --file $compose `
    exec -T database sh -lc `
    'MYSQL_PWD="$MYSQL_PASSWORD" mysql --user=ref_reader --batch --skip-column-names'
if ($LASTEXITCODE -ne 0) {
    throw '数据库只读检查失败。'
}
$joined = $output -join "`n"
foreach ($expected in @(
    "dim_date`t181",
    "dim_process`t3",
    "dim_ship`t6",
    "dim_workshop`t6",
    "fact_production_progress`t6516",
    'GRANT SELECT ON `ref_contest_db`.*'
)) {
    if (-not $joined.Contains($expected)) {
        throw "数据库检查缺少预期结果：$expected"
    }
}

Write-Host 'PASS'
Write-Host "- 前端入口：$baseUrl/"
Write-Host '- 交互 API：会话创建与 5 道岗前测评通过'
Write-Host '- 数据库：5 表行数正确，ref_reader 仅保留 SELECT'
