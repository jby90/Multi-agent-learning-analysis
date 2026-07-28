# 已有数据库版本升级说明

本目录提供当前协同重构版的后端、前端增量镜像。它适用于已经部署过旧版、已经拥有 `multiagent-decision-1-1-0_database_data` 数据卷的学生。

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

## 二、为旧版前后端镜像保留回滚标签

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

## 四、加载新版前后端镜像

```powershell
docker image load --input .\multiagent-decision-backend_1.1.0-coordination-32d3e49.tar.gz
docker image load --input .\multiagent-decision-frontend_1.1.0-coordination-32d3e49.tar.gz
```

两个压缩包内部仍使用 `multiagent-decision-backend:1.1.0` 和 `multiagent-decision-frontend:1.1.0` 标签，因此不需要修改原有 Compose 服务名。

## 五、只重建前端和后端

```powershell
docker compose --env-file .env --file docker-compose.yml up --detach --no-deps --force-recreate backend frontend
```

此命令不会重建数据库服务，也不会删除数据库卷。等待十几秒后检查：

```powershell
docker compose --env-file .env --file docker-compose.yml ps
```

`database`、`backend`、`frontend` 应均为 `healthy`。然后打开 `http://127.0.0.1:18080/`，并使用 `Ctrl+F5` 强制刷新浏览器缓存。

如果 `.env` 中修改过 `FRONTEND_HOST_PORT`，请使用对应端口。

## 六、验证新功能

1. 进入“实操通道”，选择岗位并完成岗前测评。
2. 点击“打开岗位微课”，等待三路资源和四维审核汇聚。
3. 切换到“协同视图”。
4. 确认能看到新版卡通 Agent、专业审核专项组和“协同效能证据”面板。

也可以执行：

```powershell
.\smoke-test.ps1
```

## 回滚

如果需要退回升级前版本：

```powershell
docker tag multiagent-decision-backend:backup-before-coordination multiagent-decision-backend:1.1.0
docker tag multiagent-decision-frontend:backup-before-coordination multiagent-decision-frontend:1.1.0
docker compose --env-file .env --file docker-compose.yml up --detach --no-deps --force-recreate backend frontend
```

回滚同样不会修改数据库数据卷。

## 新安装说明

这两个增量镜像不包含数据库镜像。没有部署过旧版的电脑仍需要原始完整离线包中的数据库镜像和初始化数据；已有旧版的学生无需重复下载数据库镜像。
