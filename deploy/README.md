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

## 管理员小程序

1. 确保部署站点有有效的 HTTPS 证书。使用上面的 Docker 配置时，Caddy 会把小程序页面和接口一起转发给机器人服务。自行部署时，将 HTTPS 反向代理转发到机器人的 HTTP 监听地址。
2. 在 `.env` 填写 `MINI_APP_BASE_URL=https://giveaway.example.com`。填写站点地址，程序会拼接 `/admin/`。使用现有 Docker 配置时，域名和启动时传入的 `DOMAIN` 一致。
3. 重启服务，再用管理员账号在机器人私聊中点「抽奖管理」。机器人会为配置文件和数据库中的管理员设置菜单按钮。若管理员从未私聊过机器人，先点「开始」即可。

`MINI_APP_BASE_URL` 和 `WEBHOOK_BASE_URL` 独立配置。小程序仍可用长轮询部署。退出并重新打开小程序可刷新已过期的 Telegram 登录信息。
