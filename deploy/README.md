# 部署

## 普通服务器

按项目根目录 README 创建虚拟环境、安装依赖并填写 `.env`，执行 `python -m bot`。长轮询只需服务器能够连接 Telegram，不需要域名或验证码网页。

## Docker 和 HTTPS

在项目根目录复制 `.env.example` 为 `.env` 并填写必要项，再执行：

```bash
DOMAIN=giveaway.example.com docker compose -f deploy/docker-compose.yml up -d --build
```

此方式会启动 Caddy 并占用 80、443 端口，须将域名指向服务器。若使用 Webhook，设置 `WEBHOOK_BASE_URL=https://giveaway.example.com` 和 `WEBHOOK_SECRET`。数据库持久化在项目根目录 `data/`，更新前请停止服务并备份该目录及 `.env`。

更新后执行同一条构建启动命令；查看日志：

```bash
docker compose -f deploy/docker-compose.yml logs -f bot
```

古诗词答题全部在机器人私聊内进行，无需配置 Cloudflare。
