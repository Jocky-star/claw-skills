# 当前持仓（示例）

用于「持仓占比 + 策略建议」功能。可手动维护，或从东方财富/同花顺导出持仓后整理成此格式。

## 现金（元）

```yaml
cash_cny: 50000
```

## 持仓列表

```yaml
positions:
  - code: "600519"
    name: "贵州茅台"
    shares: 100
    bucket: "core"
  - code: "000001"
    name: "平安银行"
    shares: 1000
    bucket: "value"
  - code: "512890"
    name: "红利低波ETF"
    shares: 5000
    bucket: "dividend"
```

## 可选策略覆盖

```yaml
max_single_weight: 0.30
min_cash_weight: 0.05
top3_concentration_limit: 0.70
rebalance_threshold: 0.05
asset_bucket_limits:
  core: {min: 0.20, max: 0.60}
  dividend: {min: 0.10, max: 0.40}
  gold: {max: 0.20}
```

- `code`: 6位股票代码
- `name`: 名称（可选，用于报告显示）
- `shares`: 持仓数量（股）
- `bucket`: 可选，资产桶分类（如 core / value / dividend），用于策略统计

## 从东方财富获取持仓

### 方式 A：导出后整理（最稳妥）

1. 打开东方财富客户端 → 交易 → 持仓
2. 导出持仓（若有导出功能）或手动按上表整理
3. 将本文件保存为 `current_position.md`（不要提交到 git，已加入 `.gitignore`）

### 方式 B：从券商/交易软件导出 CSV 后直接使用（推荐）

1. 在交易软件中导出持仓 CSV
2. 将 CSV 放到项目目录（例如 `holding.csv`）
3. 直接运行：

```bash
python scripts/stock_monitor.py portfolio snapshot --position-file holding.csv --out snapshot.json
```
