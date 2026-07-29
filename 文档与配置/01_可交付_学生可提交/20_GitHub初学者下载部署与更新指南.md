# GitHub 初学者下载、部署与更新指南

这份说明面向第一次使用 Git、GitHub 和 Docker 的同学。请按顺序执行，不要跳过“保留旧版本”和“.env 配置”两步。

## 1. 先认识几个词

- **仓库（Repository）**：GitHub 上存放项目代码的地方。
- **分支（Branch）**：同一项目的一条独立开发路线。本轮学生测试分支是 `agent/poll-recovery-student-test`。
- **提交（Commit）**：某一次确定的代码版本。测试报告必须记录提交号。
- **克隆（Clone）**：把 GitHub 仓库完整下载到自己的电脑，并保留后续更新能力。
- **容器（Container）**：Docker 正在运行的程序实例。本项目的旧版和新版可以同时存在。

建议使用 `git clone`，不要使用 GitHub 页面上的“Download ZIP”。ZIP 可以查看代码，但以后不方便安全更新和确认版本。

## 2. 需要准备什么

请先安装并启动：

1. Git for Windows。
2. Docker Desktop。
3. Chrome 或 Edge。
4. PowerShell（Windows 自带）。

打开 PowerShell，逐条运行：

```powershell
git --version
docker version
docker compose version
```

三条命令都能显示版本号才继续。如果 `docker version` 只显示 Client、没有 Server，请先打开 Docker Desktop，等待左下角显示 Docker Engine 正在运行。

只运行 Docker 版本不要求电脑预先安装 Node.js 或 Python；只有参与源码开发和本地自动化测试时才需要它们。

## 3. 保留原来的旧版本

不要在原项目目录里覆盖文件，也不要删除原来的 Docker 容器或数据卷。最稳妥的方式是把测试版克隆到一个新目录。

例如原版在：

```text
D:\projects\Multi-agent-learning-analysis-old
```

那么测试版可以放在：

```text
D:\projects\Multi-agent-learning-analysis-student-test
```

旧版默认仍从 `http://127.0.0.1:18080/` 访问；当前创新测试版使用 `http://127.0.0.1:18082/`，两者不会互相覆盖。

## 4. 第一次从 GitHub 下载测试分支

先在 PowerShell 进入你准备存放项目的父目录。下面以 `D:\projects` 为例：

```powershell
Set-Location D:\projects

git clone `
  --branch agent/poll-recovery-student-test `
  --single-branch `
  https://github.com/jby90/Multi-agent-learning-analysis.git `
  Multi-agent-learning-analysis-student-test

Set-Location .\Multi-agent-learning-analysis-student-test
git branch --show-current
git rev-parse --short HEAD
```

正确结果应包括：

- 当前分支：`agent/poll-recovery-student-test`
- 提交号：至少为负责人通知的测试提交或更新版本。

如果你的代码要放在其他盘符或文件夹，只替换 `D:\projects`，不要修改仓库网址和分支名。

## 5. 准备本机 `.env`

`.env` 保存数据库密码和模型 Key，Git 不会上传它。每台电脑都必须有自己的 `Docker镜像/.env`。

### 5.1 已经部署过旧版

优先从旧项目复制原 `.env`。把下方旧目录替换成你电脑上的真实路径：

```powershell
Copy-Item `
  -LiteralPath "D:\projects\Multi-agent-learning-analysis-old\Docker镜像\.env" `
  -Destination ".\Docker镜像\.env"
```

### 5.2 没有可复用的 `.env`

```powershell
Copy-Item ".\Docker镜像\.env.example" ".\Docker镜像\.env"
notepad ".\Docker镜像\.env"
```

至少需要正确填写：

- `MYSQL_ROOT_PASSWORD`：旧版数据库使用的 root 密码。
- `REF_READER_PASSWORD`：旧版只读账号 `ref_reader` 的密码。
- `DASHSCOPE_API_KEY`：本人获授权的模型 Key。

不要在群聊、截图、测试报告或 GitHub Issue 中公开这些值。保存后运行：

```powershell
git status --short
```

输出中不应出现 `Docker镜像/.env`。如果出现，先停止操作并联系负责人，不要执行 `git add`。

## 6. 确认旧数据库仍然存在

创新版不重新导入数据库，而是连接原版数据库网络。执行：

```powershell
docker network inspect multiagent-decision-1-1-0_default
docker volume ls | Select-String 'multiagent-decision-1-1-0_database_data'
```

网络和数据卷都能看到才继续。如果提示网络不存在，不要手工创建一个空网络；应先按旧版说明启动原数据库，因为空网络里没有数据库服务。

特别注意：所有 `docker compose down` 命令后面都不得附加删除数据卷的 `-v` 选项。该选项会删除 Compose 数据卷，可能导致原数据库丢失。

## 7. 第一次构建创新测试版

确认 PowerShell 当前位于仓库根目录，也就是能看到 `README.md`、`source_package` 和 `Docker镜像` 的目录。

依次执行：

```powershell
docker build `
  --file .\docker\backend\Dockerfile `
  --tag multiagent-decision-backend:innovation-ab-6e2042e .

docker build `
  --file .\docker\frontend\Dockerfile `
  --tag multiagent-decision-frontend:innovation-ab-6e2042e .
```

第一次构建需要下载基础镜像和依赖，时间可能较长。必须看到两条构建命令都以成功结束，再执行启动命令。

如果出现 Docker Hub `EOF`、`failed to fetch anonymous token` 或下载超时，通常是镜像源网络问题，不要删除数据库和项目。保持代码不变，检查网络或稍后重试构建即可。

## 8. 启动并确认 18082

```powershell
docker compose `
  --env-file .\Docker镜像\.env `
  --file .\Docker镜像\docker-compose.innovation-ab.yml `
  up --detach --wait --wait-timeout 120
```

随后检查：

```powershell
docker compose `
  --env-file .\Docker镜像\.env `
  --file .\Docker镜像\docker-compose.innovation-ab.yml `
  ps
```

前端和后端都应显示 `healthy`。然后依次打开：

- 旧版：`http://127.0.0.1:18080/`
- 协同对照版：`http://127.0.0.1:18081/`（如已部署）
- 当前创新测试版：`http://127.0.0.1:18082/`

学生测试只在 `18082` 进行，截图时也要保留浏览器地址栏，避免把三个版本混淆。

## 9. 停止和重新启动

只停止创新版：

```powershell
docker compose `
  --env-file .\Docker镜像\.env `
  --file .\Docker镜像\docker-compose.innovation-ab.yml `
  down
```

重新启动时再次执行第 8 节的 `up --detach --wait`。停止创新版不会删除旧版和数据库，但仍然不能添加 `-v`。

## 10. 老师发布新提交后如何更新

先关闭正在编辑的代码文件，在测试版仓库根目录执行：

```powershell
git status --short
git pull --ff-only
git rev-parse --short HEAD
```

如果 `git status --short` 没有输出，说明本地没有改动，可以继续更新。

如果它列出文件，说明你改过本地代码：不要运行 `git reset --hard`，也不要强行覆盖。先把输出和你修改的文件发给负责人确认。

代码更新后，为避免使用旧镜像，重新构建并启动：

```powershell
docker build --file .\docker\backend\Dockerfile --tag multiagent-decision-backend:innovation-ab-6e2042e .
docker build --file .\docker\frontend\Dockerfile --tag multiagent-decision-frontend:innovation-ab-6e2042e .

docker compose `
  --env-file .\Docker镜像\.env `
  --file .\Docker镜像\docker-compose.innovation-ab.yml `
  up --detach --force-recreate --wait --wait-timeout 120
```

最后在浏览器按 `Ctrl+F5` 强制刷新，并重新记录提交号。

## 11. 开始全流程测试

部署成功后，严格按照 [19_学生全流程探索测试清单](19_学生全流程探索测试清单.md) 分组测试。不要只确认首页能够打开，应至少完成：

1. 一种岗位画像的完整训练流程。
2. 临时断网后会话自动恢复。
3. Agent 协同状态和并行审核观察。
4. 合法 SQL 与安全拒绝测试。
5. 刷新、关闭重开、双击和双窗口会话隔离。

正式 50×2 不由每位学生重复执行，最终冻结后由指定负责人统一运行。

## 12. 如何提交 Bug

1. 先更新到老师指定的最新提交。
2. 同一个问题至少重复 3 次，记录“出现次数/测试次数”。
3. 打开 GitHub 仓库的 `Issues` 页面，点击 `New issue`。
4. 标题建议写成：`[S1][Edge][18082] 短暂断网后回到岗位选择页`。
5. 复制测试清单第 14 节的模板，填写复现步骤、预期结果和实际结果。
6. 附上脱敏截图、短视频、Console 错误和 Network 状态码。

报告前必须删除或遮挡 Key、密码、`.env` 内容和未脱敏数据。不要只写“不能用”或只发一张截图；没有步骤和提交号的问题很难复现。

## 13. 常见问题

### `git` 不是内部或外部命令

Git for Windows 尚未安装，或安装后 PowerShell 没有重新打开。安装 Git 后关闭并重新打开 PowerShell。

### 无法连接 Docker

确认 Docker Desktop 已启动，并再次运行 `docker version`。如果没有 Server 信息，Docker Engine 还没有准备好。

### `network multiagent-decision-1-1-0_default declared as external, but could not be found`

旧版数据库网络没有启动。先启动旧版数据库，不要用 `docker network create` 制造一个没有数据库的空网络。

### `port is already allocated`

默认端口被其他程序占用。可以在 `Docker镜像/.env` 末尾增加：

```dotenv
INNOVATION_AB_FRONTEND_HOST_PORT=28082
INNOVATION_AB_BACKEND_HOST_PORT=28767
```

重新启动后改为访问 `http://127.0.0.1:28082/`，并在 Bug 报告中写明自定义端口。

### 页面还是旧样式或旧逻辑

确认已执行 `git pull --ff-only`、重新构建前端镜像、`--force-recreate` 启动，并在浏览器按 `Ctrl+F5`。

### 容器不是 `healthy`

先保存日志：

```powershell
docker compose `
  --env-file .\Docker镜像\.env `
  --file .\Docker镜像\docker-compose.innovation-ab.yml `
  logs --tail 200
```

把日志发生时间、服务名和脱敏后的错误一起提交，不要直接删除容器或数据卷。

## 14. 成功检查表

- [ ] 使用的是独立测试目录，旧版目录仍保留。
- [ ] 当前分支为 `agent/poll-recovery-student-test`。
- [ ] 已记录 `git rev-parse --short HEAD`。
- [ ] `.env` 存在但没有被 Git 跟踪。
- [ ] 原数据库网络和数据卷存在。
- [ ] 创新版前后端均为 `healthy`。
- [ ] `18080` 旧版和 `18082` 创新版可以区分。
- [ ] 已阅读 19 号全流程测试清单。
- [ ] Bug 报告不包含任何密钥或密码。
