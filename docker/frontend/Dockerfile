FROM node:22-alpine AS builder

WORKDIR /workspace/source_package/frontend
COPY source_package/frontend/package.json source_package/frontend/package-lock.json ./
RUN npm ci
COPY source_package/agents/ /workspace/source_package/agents/
COPY source_package/config/ /workspace/source_package/config/
COPY source_package/traces/ /workspace/source_package/traces/
COPY source_package/frontend/ ./
RUN npm run build

FROM nginx:1.29-alpine
COPY Docker镜像/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /workspace/source_package/frontend/dist/ /usr/share/nginx/html/

EXPOSE 80
