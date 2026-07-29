# 保留旧版并并行运行新版

## 创新 A+B 源码版（新增，推荐开发组使用）

创新 A+B 冻结源码对应 Git 标签 `innovation-ab-code-freeze-20260729`、提交 `6e2042e`。它使用独立 Compose 和端口，不覆盖下面的 1.1.0 旧版或 `coordination-32d3e49` 协同重构版。

在仓库根目录、Docker Hub 可访问时构建：

```powershell
docker build --file .\docker\backend\Dockerfile --tag multiagent-decision-backend:innovation-ab-6e2042e .
docker build --file .\docker\frontend\Dockerfile --tag multiagent-decision-frontend:innovation-ab-6e2042e .
```

随后启动独立创新版：

```powershell
docker compose --env-file .\Docker镜像\.env --file .\Docker镜像\docker-compose.innovation-ab.yml up --detach --wait --wait-timeout 120
```

默认入口为：

- 旧版 1.1.0：`http://127.0.0.1:18080/`
- 原协同重构版：`http://127.0.0.1:18081/`
- 创新 A+B 版：`http://127.0.0.1:18082/`

创新版只复用旧版数据库网络和只读账号，拥有独立前后端容器、镜像标签与 runtime 卷。若端口冲突，可在 `.env` 中设置 `INNOVATION_AB_FRONTEND_HOST_PORT` 与 `INNOVATION_AB_BACKEND_HOST_PORT`。

只停止创新版且保留另外两版：

```powershell
docker compose --env-file .\Docker镜像\.env --file .\Docker镜像\docker-compose.innovation-ab.yml down
```

不要附加 `-v`，也不要对旧版 Compose 执行 `down -v`。

下面内容继续保留，作为 `coordination-32d3e49` 历史协同重构版的离线镜像部署说明。

本目录提供当前协同重构版的后端、前端增量镜像。它适用于已经部署过旧版、已经拥有 `multiagent-decision-1-1-0_database_data` 数据卷的学生。推荐保留旧版并在另一组端口同时运行新版。

本次没有修改数据库结构和比赛数据，因此不需要重新加载数据库镜像，也不需要重新导入 CSV。

## 升级前提

- Docker Desktop 正常运行。
- 旧版系统已经部署，数据库容器或数据库数据卷仍然存在。
- 原有 `.env` 仍在本目录中，并包含学生自己的数据库密码和模型 API Key。
- 已执行 `git pull`，本目录中能看到两个 `coordination-32d3e49.tar.gz` 文件。

## 一、确认数据库卷存在

在本目录执行：

```powershell
docker volume ls | Select-String 'multiagent-decision-1-1-0_database_data'
```

能看到该数据卷后再继续。升级过程中不要执行 `docker compose down -v`，也不要运行 `./stop.ps1 -RemoveData`。

## 二、永久保留旧版镜像标签

```powershell
docker tag multiagent-decision-backend:1.1.0 multiagent-decision-backend:backup-before-coordination
docker tag multiagent-decision-frontend:1.1.0 multiagent-decision-frontend:backup-before-coordination
```

这一步只给旧镜像增加标签，不会停止正在运行的容器。

## 三、核对升级文件

升级文件及 SHA-256 值记录在 `SHA256SUMS_UPGRADE_32d3e49.txt`。可以执行：

```powershell
Get-FileHash -Algorithm SHA256 .\multiagent-decision-backend_1.1.0-coordination-32d3e49.tar.gz
Get-FileHash -Algorithm SHA256 .\multiagent-decision-frontend_1.1.0-coordination-32d3e49.tar.gz
```

计算结果应与校验文件一致。

## 四、加载新版并建立独立标签

```powershell
docker image load --input .\multiagent-decision-backend_1.1.0-coordination-32d3e49.tar.gz
docker image load --input .\multiagent-decision-frontend_1.1.0-coordination-32d3e49.tar.gz

docker tag multiagent-decision-backend:1.1.0 multiagent-decision-backend:coordination-32d3e49
docker tag multiagent-decision-frontend:1.1.0 multiagent-decision-frontend:coordination-32d3e49

docker tag multiagent-decision-backend:backup-before-coordination multiagent-decision-backend:1.1.0
docker tag multiagent-decision-frontend:backup-before-coordination multiagent-decision-frontend:1.1.0
```

加载时压缩包会临时占用 `1.1.0` 标签。后四条命令先把新版保存为 `coordination-32d3e49`，再把 `1.1.0` 恢复指向旧版。这样旧版 Compose 以后重新启动时仍会使用旧镜像。

## 五、保持旧版运行并启动新版

```powershell
docker compose --env-file .env --file docker-compose.coordination.yml up --detach --wait --wait-timeout 120
```

新版 Compose 不包含数据库服务。新版后端会通过原有 Docker 网络连接旧版数据库，并且仍然只使用 `ref_reader` 只读账号。等待启动完成后检查：

```powershell
docker compose --env-file .env --file docker-compose.yml ps
docker compose --env-file .env --file docker-compose.coordination.yml ps
```

两组服务应均为 `healthy`：

- 旧版：`http://127.0.0.1:18080/`
- 协同重构版：`http://127.0.0.1:18081/`

两版前后端容器、镜像标签和运行记录相互独立，只有比赛数据库是共享的。数据库账号为只读，因此两套系统不会互相修改业务数据。

如果 18081 或 18766 已被占用，可在 `.env` 中增加 `COORDINATION_FRONTEND_HOST_PORT` 或 `COORDINATION_BACKEND_HOST_PORT` 自定义新版端口。

## 六、验证新功能

1. 进入“实操通道”，选择岗位并完成岗前测评。
2. 点击“打开岗位微课”，等待三路资源和四维审核汇聚。
3. 切换到“协同视图”。
4. 确认能看到新版卡通 Agent、专业审核专项组和“协同效能证据”面板。

原有 `smoke-test.ps1` 默认检查旧版 18080。新版可直接打开 18081，完成一次会话创建和岗前测评进行验证。

## 单独停止新版

停止协同重构版但保留旧版：

```powershell
docker compose --env-file .env --file docker-compose.coordination.yml down
```

不要附加 `-v`。上述命令不会停止旧版，也不会删除旧版数据库卷。

## 新安装说明

这两个增量镜像不包含数据库镜像。没有部署过旧版的电脑仍需要原始完整离线包中的数据库镜像和初始化数据；已有旧版的学生无需重复下载数据库镜像。
