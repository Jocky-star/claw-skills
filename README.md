# A股智能盯盘助手 (A-Share Monitor)

> 让 AI 帮你盯盘 — 即时分析 + 持续监控 + 持仓占比策略建议。

### 实际效果

对 OpenClaw 说 **"帮我盯一下贵州茅台"**，它会：

1. **立刻**给你一份实时行情 + 技术信号 + 综合研判报告
2. **然后问你**要不要继续盯——可以选「每 10 分钟推一次行情」或「只在出现强信号时通知我」
3. 自动识别交易时段，**午休和收盘后自动暂停**，不刷废数据

对 OpenClaw 说 **"按我的持仓给建议"**，它会：

1. 读取你维护的持仓文件（`current_position.md` / CSV）
2. 计算持仓占比、前 3 集中度、现金占比
3. 叠加技术面信号（MACD/KDJ/均线/RSI/TD Buy9 等）与资产桶约束输出建议

## ⚡ 一键安装（推荐）

直接把以下内容发送给你的 OpenClaw：

```
请帮我安装这个 Skill：https://github.com/Jocky-star/claw-skills.git

安装完成后，进入 a-share-monitor 目录，运行 scripts/setup.sh 完成环境配置。
```

OpenClaw 会自动 clone、安装依赖、部署技能文件。装完就能用，无需任何配置。

## 🛠️ 手动安装

```bash
git clone https://github.com/Jocky-star/claw-skills.git
bash claw-skills/a-share-monitor/scripts/setup.sh
```

## 💬 使用

安装后直接对 OpenClaw 说：

| 你说的话 | AI 做什么 |
|----------|-----------|
| 帮我盯一下贵州茅台 | 即时报告 → 询问是否继续盯盘 |
| 分析 300750 宁德时代 | MACD/KDJ/RSI/布林带等全套信号扫描 |
| 看看 000001 有什么信号 | 只列出触发的信号，忽略无信号指标 |

报告出来后，AI 会问你要不要继续盯：

| 盯盘模式 | 说明 |
|----------|------|
| **定时播报** | 开盘期间每 10 分钟推送一次完整行情快报 |
| **信号盯盘** | 后台持续监控，只在出现强烈信号或价格剧烈波动时才通知 |

触发词：`盯盘`、`技术分析`、`信号`、`股票监控`、`A股`、股票代码或名称

## ⏰ 自动提醒说明

- 默认行为是**被动触发**：安装后不会自动在开盘时主动推送。
- 你需要配置定时任务，按固定时间触发 skill 生成建议。
- 进入 `monitor` 后，脚本会在非交易时段自动暂停并等待。

示例（如果你的 OpenClaw 支持 cron）：

```bash
# 工作日 09:30 开盘提醒
openclaw cron add --name "AShare Open Advice" \
  --expr "30 9 * * 1-5" \
  --tz "Asia/Shanghai" \
  --message "运行 a-share-monitor：读取 current_position.md 并生成今日操作建议"

# 工作日 14:50 收盘前提醒
openclaw cron add --name "AShare PreClose Advice" \
  --expr "50 14 * * 1-5" \
  --tz "Asia/Shanghai" \
  --message "运行 a-share-monitor：给出收盘前仓位与风险建议"
```

## 🧮 持仓占比与策略建议

命令行直跑（PowerShell）：

```powershell
python .\scripts\stock_monitor.py portfolio init --position-file .\current_position.md
python .\scripts\stock_monitor.py portfolio strategy --position-file .\current_position.md
python .\scripts\stock_monitor.py portfolio trade --position-file .\current_position.md --action buy --code 600519 --shares 100 --name 贵州茅台 --bucket core
python .\scripts\stock_monitor.py portfolio snapshot --position-file .\current_position.md --out .\snapshot.json
python .\scripts\stock_monitor.py portfolio advice --position-file .\current_position.md --technical-days 120 --top-n 5
```

可选参数：

- `--max-single-weight`：单票上限（默认 0.30）
- `--min-cash-weight`：现金下限（默认 0.05）
- `--top3-limit`：前三集中度上限（默认 0.70）
- `--rebalance-threshold`：再平衡阈值（默认 0.05）
- `--technical-days`：技术面回溯天数（默认 120）
- `--technical-top-n`：仅对市值前 N 个持仓叠加技术分析（默认 5）
- `--no-technical`：关闭技术面叠加，只保留仓位闸门
- `portfolio advice`：生成“今天优先执行 / 可以考虑 / 继续观察”的每日建议

默认策略（可在 `current_position.md` 覆盖）：

- `max_single_weight=0.30`
- `min_cash_weight=0.05`
- `top3_concentration_limit=0.70`
- `rebalance_threshold=0.05`

## 📊 支持的技术信号

均线交叉（金叉/死叉/多空排列）· MACD（金叉/死叉/顶底背离）· KDJ 超买超卖 · RSI · 布林带突破与收口 · 成交量异动 · K线形态（十字星/锤子线/吞没）· TD Setup 9（Buy9/Sell9）· 涨跌幅预警 · 支撑压力位 · 综合多空研判

## 🔧 数据源

新浪财经（实时行情 + K线历史）+ akshare（股票搜索）。行情部分无需 API Key。

## 🕐 交易时段感知

| 时段 | 行为 |
|------|------|
| 9:30-11:30 / 13:00-15:00 | 正常获取实时数据、推送报告/信号 |
| 9:15-9:25 集合竞价 | 提示数据可能不稳定 |
| 11:30-13:00 午休 | 自动暂停监控，等待下午开盘 |
| 15:00 后 / 周末 | 自动暂停，使用最近交易日收盘数据 |

## 📁 文件结构

```
a-share-monitor/
├── SKILL.md                       # OpenClaw 技能指令
├── README.md                      # 本文档
├── openclaw.plugin.json           # 插件清单
├── scripts/
│   ├── stock_monitor.py           # 核心引擎（分析 + 持续盯盘）
│   └── setup.sh                   # 一键安装脚本（venv 隔离）
└── references/
    └── signal_glossary.md         # 信号术语表
```

## 🔐 安全说明

- 仅读取公开行情与用户维护的持仓文件，不涉及任何交易操作
- 不需要托管任何券商账号密码或登录态
- 源代码完全开源，可自行审查

## ⚖️ 免责声明

本技能仅用于技术分析信息展示，**不构成任何投资建议**。股市有风险，投资需谨慎。所有信号仅供参考，使用者对自己的投资决策负全部责任。

## License

MIT
