---
name: a-share-monitor
description: "当用户想要监控A股行情、查看技术指标信号、按持仓文件生成策略建议时使用此技能"
user-invocable: true
metadata:
  openclaw:
    requires:
      bins: ["python3"]
      env: []
      config: []
---

# A股智能盯盘助手

实时监控用户指定的A股股票，自动检测经典技术分析信号；并支持读取用户维护的持仓文件生成持仓占比与策略建议报告。

## When to Use

当用户发出以下类型的请求时激活：

- "帮我盯一下 XXX" / "监控一下 600519"
- "看看 XXX 有什么信号" / "分析一下 XXX 技术面"
- "盯盘报告" / "股票监控"
- "按我当前持仓做仓位建议"

## Required Inputs

| 输入 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `stock_code` | string | 是 | 6位股票代码，如 `600519`。用户给名称时需先搜索解析 |
| `days` | int | 否 | 回溯交易日数，默认 `120` |
| `position_file` | string | 否 | 本地持仓文件路径，默认 `current_position.md` |

## 运行环境

依赖通过 venv 隔离安装。技能部署后目录结构如下：
```
~/.openclaw/skills/a-share-monitor/
├── .venv/          # Python 虚拟环境（setup.sh 自动创建）
├── scripts/
│   └── stock_monitor.py
└── ...
```

所有 python 命令统一使用 venv 中的解释器：
```bash
VENV_PYTHON="$HOME/.openclaw/skills/a-share-monitor/.venv/bin/python"
```

**后续所有步骤中的 `python` 均指 `$VENV_PYTHON`。如果 `.venv` 不存在，先运行 `scripts/setup.sh` 或手动创建：**
```bash
python3 -m venv ~/.openclaw/skills/a-share-monitor/.venv
~/.openclaw/skills/a-share-monitor/.venv/bin/python -m pip install akshare pandas ta requests
```

## Step-by-Step Workflow

### 触发方式说明（重要）

- 默认是**被动触发**：用户问一句，skill 执行一句，不会安装后自动开盘推送。
- 若用户希望"开盘自动提醒"，需要额外配置定时任务（cron/计划任务）去触发本 skill。
- 一旦进入 `monitor` 模式，脚本会自动识别交易时段并在非交易时段暂停。

### 第一步：解析用户输入

1. 提取股票代码或名称
2. 如果是名称，用 **十六进制编码搜索** 或 **代码搜索** 将名称解析为代码：
   ```bash
   # 十六进制搜索（将中文 UTF-8 字节转为 hex 字符串）
   $VENV_PYTHON scripts/stock_monitor.py search --keyword-hex e88c85e58fb0
   # 代码搜索
   $VENV_PYTHON scripts/stock_monitor.py search --code 600519
   ```
   也可以用文件传入关键字（先用文件写入工具创建 UTF-8 文件）：
   ```bash
   $VENV_PYTHON scripts/stock_monitor.py search --keyword-file /tmp/keyword.txt
   ```
3. 市场自动判断：600/601/603/688 → 沪市，000/002/300 → 深市，8/4 → 北交所
4. 无法确认时 **立即向用户确认**

### 第二步：获取行情与技术分析

```bash
$VENV_PYTHON scripts/stock_monitor.py analyze <stock_code> --days <days>
```

脚本输出 JSON，包含实时行情、全部指标值、触发的信号列表。

### 第三步：生成即时报告

根据 JSON 数据生成中文报告。**只报告有意义的信号，不要列出所有指标水篇幅。**

注意 JSON 中的 `market_status` 字段：
- `trading` → 数据为实时行情，正常出报告
- `auction` → 提示「集合竞价中，数据可能不稳定」
- `break` / `closed` → 明确标注「当前{display}，以下为最近交易日数据」

```
📊 【{股票名称}（{股票代码}）盯盘报告】
⏰ 数据时间：{timestamp}  |  市场状态：{market_status.display}

━━━━━━━ 行情概览 ━━━━━━━
现价：{price}  |  涨跌幅：{change_pct}%
今开：{open}  |  昨收：{pre_close}
最高：{high}  |  最低：{low}
成交量：{volume}手  |  成交额：{amount}万

━━━━━━━ 信号雷达 ━━━━━━━
🔴/🟢 {信号等级} | {信号名称}
   └─ {信号描述与数值}

━━━━━━━ 综合研判 ━━━━━━━
多空信号：🟢 看多 {n} 个 | 🔴 看空 {n} 个 | ⚪ 中性 {n} 个
综合倾向：{偏多/偏空/震荡中性}
关键位置：上方压力 {resistance} | 下方支撑 {support}

⚠️ 以上分析仅基于技术指标，不构成投资建议。

━━━━━━━ 后续盯盘 ━━━━━━━
要继续盯盘吗？回复 A / B / C：
  A. 📡 定时播报 - 开盘期间每10分钟推送一次完整行情快报
  B. 🔔 信号盯盘 - 后台持续监控，只在出现强烈信号时通知你
  C. ✅ 不用了 - 只看这一次
```

**⚠️ 重要：上面的「后续盯盘」选项是报告模板的一部分。每次输出即时报告时，必须在报告末尾、风险提示之后，原样附上这段 A/B/C 选项。不可省略。**

### 第三步-B：持仓占比与策略建议（持仓文件）

当用户请求"按当前持仓给建议"时，执行：

```bash
$VENV_PYTHON scripts/stock_monitor.py portfolio init --position-file current_position.md
```

用户后续有交易变更时，优先用结构化命令更新持仓文件：

```bash
$VENV_PYTHON scripts/stock_monitor.py portfolio trade --position-file current_position.md --action buy --code 600519 --shares 100 --name 贵州茅台 --bucket core
$VENV_PYTHON scripts/stock_monitor.py portfolio trade --position-file current_position.md --action sell --code 600519 --shares 100
$VENV_PYTHON scripts/stock_monitor.py portfolio trade --position-file current_position.md --action set-cash --cash-set 120000
```

再生成快照与建议：

```bash
$VENV_PYTHON scripts/stock_monitor.py portfolio snapshot \
  --position-file current_position.md \
  --technical-days 120 \
  --technical-top-n 5 \
  --out snapshot.json
```

如用户问"今天该怎么操作"，优先用每日建议命令：

```bash
$VENV_PYTHON scripts/stock_monitor.py portfolio advice \
  --position-file current_position.md \
  --technical-days 120 \
  --top-n 5
```

查看"默认策略 + 用户覆盖策略 + 生效策略"：

```bash
$VENV_PYTHON scripts/stock_monitor.py portfolio strategy --position-file current_position.md
```

说明：
- 用户负责维护 `current_position.md`（或导出 CSV），skill 仅负责行情与策略计算
- 输出为 Markdown 报告 + 可选 JSON 快照，包含：持仓占比、资产桶占比、触发闸门、技术面叠加、操作建议
- `portfolio advice` 输出"今天优先执行 / 可以考虑 / 继续观察"三段建议，便于用户直接执行
- 默认策略（参考 portfolio-skill 的规则化风格）：
  - `max_single_weight=0.30`
  - `min_cash_weight=0.05`
  - `top3_concentration_limit=0.70`
  - `rebalance_threshold=0.05`
  - 盯盘信号中默认纳入 `TD Buy 9 / TD Sell 9`（九转）辅助判断

### 第三步-C：配置自动提醒（可选）

如果用户明确希望"每天开盘自动给建议"，可引导其配置定时触发：

```bash
# 示例：工作日 09:30 触发每日建议
openclaw cron add --name "AShare Open Advice" \
  --expr "30 9 * * 1-5" \
  --tz "Asia/Shanghai" \
  --message "运行 a-share-monitor：读取 current_position.md 并生成今日操作建议"
```

```bash
# 示例：工作日 14:50 再触发一次（收盘前）
openclaw cron add --name "AShare PreClose Advice" \
  --expr "50 14 * * 1-5" \
  --tz "Asia/Shanghai" \
  --message "运行 a-share-monitor：给出收盘前仓位与风险建议"
```

### 信号分级与综合研判

**每只股票必须给出一个明确的操作信号，不能只罗列红绿信号让用户自己判断。**

权重规则：strong 信号 ±3 分，normal 信号 ±1 分，neutral 0 分。

| 综合得分 | 操作信号 | 图标 | 用户看到的结论 |
|----------|---------|------|---------------|
| ≥ +6 | 强烈看多 | 🟢🔥 | 趋势向上，可积极加仓 |
| +3 ~ +5 | 看多 | 🟢 | 偏多，可适量加仓或持有 |
| -2 ~ +2 | 震荡观望 | ⚪ | 多空交织，建议持有不动 |
| -5 ~ -3 | 看空 | 🔴 | 偏空，可考虑减仓 |
| ≤ -6 | 强烈看空 | 🔴⚠️ | 趋势向下，建议减仓止损 |

特殊修正：超买降级、超跌标注反弹可能、放量增信、均线趋势优先。详见 `strategy/a_share_simple.md`。

### 第四步：根据用户选择启动盯盘

用户回复 A / B / C 后的处理：

#### 选 A - 定时播报（periodic 模式）

```bash
$VENV_PYTHON scripts/stock_monitor.py monitor <stock_code> --mode periodic --interval 600
```

- 以后台方式运行此命令
- 脚本每 600 秒（10 分钟）输出一行 JSON 事件到 stdout
- 事件格式：`{"event": "report", "time": "...", "quote": {...}, "signals": [...], ...}`
- 收到 `report` 事件后，按上方模板格式化并推送给用户
- 非交易时段（午休/收盘/周末）脚本自动暂停，输出 `waiting` 事件，无需干预
- 用户说「停止盯盘」时终止进程

#### 选 B - 信号盯盘（signal 模式）

```bash
$VENV_PYTHON scripts/stock_monitor.py monitor <stock_code> --mode signal --interval 300
```

- 以后台方式运行此命令
- 脚本每 300 秒（5 分钟）检查一次，但 **只在以下情况输出 `alert` 事件**：
  - 出现新的 **强信号**（strength=strong）：如 MACD 金叉/死叉、均线多空排列、KDJ 超买超卖区金叉/死叉、MACD 背离、看涨/看跌吞没、巨量异动、大幅波动等
  - **价格较上次提醒变动超过 2%**
- `alert` 事件中额外包含 `new_strong_signals`（新增的强信号列表）和 `price_jump`（是否价格剧变）
- 收到 `alert` 事件时，格式化为**精简的关注提醒**（不用完整报告格式，突出新信号即可）：

```
⚡ 【{股票名称}】信号提醒
⏰ {time}  |  现价 {price}（{change_pct}%）
🔔 新增强信号：
   • {signal_1}
   • {signal_2}
⚠️ 以上仅为技术指标提醒，不构成投资建议。
```

- 脚本每 5 轮（约 25 分钟）输出一次 `heartbeat` 心跳事件，表示仍在运行，无需推送给用户
- 非交易时段自动暂停

#### 选 C - 不盯了

不启动 monitor，结束本次交互。

### monitor 事件类型速查

| event | 含义 | 是否推送给用户 |
|-------|------|---------------|
| `monitor_start` | 盯盘启动 | 是（告知已开始） |
| `report` | 定时播报（periodic 模式） | 是（格式化为完整报告） |
| `alert` | 强信号提醒（signal 模式） | 是（格式化为精简提醒） |
| `heartbeat` | 心跳（signal 模式） | 否 |
| `waiting` | 非交易时段等待 | 否（首次可告知「收盘了，明天继续」） |
| `error` | 数据获取出错 | 连续 3 次以上才告知用户 |
| `monitor_stop` | 盯盘停止 | 是 |

## Output Format

- `analyze` 命令：输出单个 JSON，转换为即时报告
- `monitor` 命令：输出 JSONL（每行一个 JSON 事件），按事件类型分别处理

## Error Handling

1. **代码无效** → 提示检查，给出相似建议
2. **非交易时间** → analyze 仍可执行，报告中标注 `market_status`；monitor 自动暂停等待
3. **网络错误** → 自动重试 3 次，仍失败则告知用户
4. **依赖缺失** → 用 venv 中的 pip 安装：`$VENV_PYTHON -m pip install akshare pandas ta requests`；若 `.venv` 不存在则先创建

## A股交易规则（生成建议前必须遵守）

**所有操作建议必须符合 A 股市场实际交易规则，详见 `strategy/a_share_simple.md` 中的「A股交易规则约束」章节。**

核心要点速查：
1. **最小交易单位 100 股（1手）**：买卖建议的股数必须是 100 的整数倍，不得出现"减仓20股"等非法操作
2. **高价股可行性检查**：如果 1 手市值已超过策略阈值对应的金额，说明无法精细调仓，应给替代方案而不是建议减仓
3. **T+1**：当日买入次日才能卖出，不得建议同日买卖
4. **涨跌停**：注明可能无法成交的情况
5. **交易费用**：调仓金额太小（<5000元）时不建议操作
6. **具体数量**：每条操作建议必须给出具体股数和预估金额

## Guardrails

- 操作建议必须基于可解释规则（仓位上限、现金下限、集中度、技术信号），不得给出"保证收益"式承诺
- **操作建议必须符合 A 股交易规则**（最小交易单位、T+1、涨跌停等），不符合规则的建议不得输出
- 每次报告/提醒末尾必须包含风险提示
- 仅限 A 股，不处理港股、美股、期货、加密货币
- 不执行任何交易操作；不接入任何券商账户登录态
