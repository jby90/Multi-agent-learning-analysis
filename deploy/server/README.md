# 在线服务器部署

本目录用于独立部署当前创新版，不依赖旧版 Compose 网络。

## 首次部署

1. 安装 Docker 和 Docker Compose。
2. 将 `multiagent-decision-database:1.1.0` 数据库镜像导入服务器。
3. 将 `.env.example` 复制为 `.env`，填入三个密钥并执行 `chmod 600 .env`。
4. 执行：

   ```bash
   ./deploy/server/update.sh
   ```

默认只向公网开放前端的 `80` 端口。MySQL 和后端 API 仅在 Docker 内部网络可访问。

## 高频更新

开发机将代码推送到 `agent/frontend-apple-refactor` 后，在服务器执行：

```bash
cd /opt/multiagent/Multi-agent-learning-analysis
./deploy/server/update.sh
```

脚本会执行快进拉取、增量构建、容器健康检查；失败时恢复上一次前后端镜像和提交。数据库卷与后端运行记录不会因更新而删除。

如果服务器直连 GitHub 不稳定，可在 Windows 开发机的项目根目录执行：

```powershell
.\deploy\server\publish.ps1
```

该脚本只上传服务器当前提交之后的增量 Git bundle，并在开发机使用 Docker 缓存构建前后端镜像，再通过 SSH 加载、健康检查和切换。该通道不依赖服务器访问 GitHub 或 Docker Hub。

## 查看状态与日志

```bash
docker compose --env-file deploy/server/.env --file deploy/server/docker-compose.yml ps
docker compose --env-file deploy/server/.env --file deploy/server/docker-compose.yml logs --tail 200 backend
```

不要把 `deploy/server/.env` 提交到 Git。
