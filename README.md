# Telegram 古诗词抽奖机器人

基于 Python 3.11+、aiogram 3、SQLAlchemy、SQLite 和 APScheduler，界面仅支持简体中文。

## 报名规则

1. 用户先私聊机器人发送 `/start`，然后点击频道抽奖帖的「参加」。
2. 机器人私聊发送一道古诗词题：诗句留出两个字，提供四个双字选项。
3. 选择正确答案即自动完成本场报名，无需再次点击参加。报名时仍会检查必加频道、黑名单和截止时间。
4. 答错会扣除一次机会并自动更换题目；每人每场最多答错五次。第五次答错后无法参与这场抽奖，其他抽奖单独计数。
5. 内置 50 道题，同一用户跨场次随机轮换，50 道用完前不重复；下一轮首题不会和上一轮末题相同。重复点击参加只重发当前题，不提供免费换题。
6. 题目、选项、错题次数、轮换进度和一次性按钮凭据保存在数据库中。重启不会恢复机会，旧题按钮无法再次作答，转发题目无法代替别人报名。

题库位于 `bot/poetry.py`，固定编号，请勿在活动进行中修改编号或答案。每题包含作品、作者、填空诗句、一个答案及三个干扰项。

古诗词答题用于增加自动报名的成本；公开固定题库无法阻止掌握题库或使用语言模型的脚本，不能视为绝对防脚本措施。

## 部署

```bash
git clone https://github.com/hikling/tg-giveaway-bot.git
cd tg-giveaway-bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填写 BOT_TOKEN 和 ADMIN_IDS
python -m bot
```

默认使用长轮询，不需要域名、公网验证页面或 Cloudflare 配置。Webhook 可选，配置 `WEBHOOK_BASE_URL` 时必须同时设置至少 8 位的 `WEBHOOK_SECRET`。Docker 部署参见 `deploy/README.md`。

升级前停止旧实例并备份 `data/` 和 `.env`；更新代码、安装依赖后启动。启动时自动创建答题记录表，保留旧抽奖、报名及领奖数据。旧 Turnstile 配置不再使用，历史抽奖也统一使用古诗词答题；已报名用户无需重答。历史数据库中旧验证相关列保留用于兼容，不再参与认证。`DEFAULT_LANG=en` 等历史设置会自动归一为中文。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `/start`、`/help` | 欢迎与帮助 |
| `/me` | 近期报名记录 |
| `/newgiveaway` | 管理员创建抽奖，确认时发送「确认」 |
| `/list`、`/cancel <编号>` | 查看、取消抽奖 |
| `/reroll <编号> <奖项名>` | 对已结束抽奖重抽 |
| `/retry <编号>` | 补发失败的中奖私信 |
| `/ban <用户编号> [原因]`、`/unban <用户编号>` | 黑名单管理 |
| `/addadmin <用户编号>`、`/admins` | 管理员管理 |
| `/gate [抽奖编号]` | 查看附加编号门槛及报名分布 |
| `/addcodes <编号> <奖项名>`、`/prizestatus <编号>` | 补货、查看奖池 |
| `/claim <编号>`、`/myprizes` | 领取、回看奖品 |

独立码每名中奖者领取一条；共享文本所有中奖者领取相同内容；也可由主办方自行联系。开奖、重抽和领奖均使用数据库事务，失败私信可补发。机器人必须是必加频道的管理员，才能可靠检查成员状态。

## 可验证开奖

发布时公布 `sha256(seed)`，开奖时揭示种子、轮次及参与者摘要。先排除黑名单，再按奖项顺序排除之前已中奖的用户，将剩余用户按编号升序排列，调用 `bot.utils.antiabuse.draw_indices(f"{seed}|{奖项名}", 人数, 名额)`。重抽使用 `f"{seed}|{奖项名}|r{轮次}"`，排除重抽前所有当前中奖者。重放需要对应时刻的参与者名单及排除名单；摘要本身不能恢复名单。

## 验证

```bash
pip install pytest pytest-asyncio
pytest
pytest hardening
```

测试使用临时 SQLite 数据库和模拟 Telegram，不需要真实机器人令牌，也不连接 Telegram。服务提供 `/healthz` 健康检查。适合单机单实例部署。

本项目沿用原项目 MIT 许可证，保留作者版权声明。
