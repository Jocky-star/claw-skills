#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股智能盯盘助手 - 技术分析引擎

数据源: 新浪财经 (实时行情 + 历史K线) + akshare (股票名称搜索)
依赖:   pip install akshare pandas ta requests
用法:
  python stock_monitor.py search --keyword-hex e88c85e58fb0
  python stock_monitor.py search --code 600519
  python stock_monitor.py analyze 600519 --days 60
  python stock_monitor.py monitor 600519 --mode periodic --interval 600
  python stock_monitor.py monitor 600519 --mode signal --interval 300
  python stock_monitor.py portfolio init --position-file current_position.md
  python stock_monitor.py portfolio trade --position-file current_position.md --action buy --code 600519 --shares 100
  python stock_monitor.py portfolio snapshot --position-file current_position.md --out snapshot.json
  python stock_monitor.py portfolio advice --position-file current_position.md
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

try:
    import yaml
except Exception:
    yaml = None

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import requests
import ta


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def retry(fn, retries=3, delay=2):
    last_err = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if i < retries - 1:
                print(f"[retry] 第{i+1}次失败({e.__class__.__name__})，{delay}s后重试…", file=sys.stderr)
                time.sleep(delay)
    raise last_err


def safe_float(val, default=0.0):
    try:
        v = float(val)
        return v if pd.notna(v) else default
    except (TypeError, ValueError):
        return default


SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}
EASTMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://jywg.18.cn/",
    "Accept": "application/json, text/plain, */*",
}
DEFAULT_EASTMONEY_COOKIE_FILE = ".eastmoney_cookie"
DEFAULT_EASTMONEY_SESSION_FILE = ".eastmoney_session.json"


# ---------------------------------------------------------------------------
# 市场判断
# ---------------------------------------------------------------------------

def detect_market(code: str) -> str:
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return "sh"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return "sz"
    if code.startswith(("8", "4")):
        return "bj"
    return "sh"


# ---------------------------------------------------------------------------
# 交易时段判断
# ---------------------------------------------------------------------------

def get_market_status() -> dict:
    """判断当前 A 股市场状态，返回 status / reason / display。"""
    now = datetime.now()
    hm = (now.hour, now.minute)

    if now.weekday() >= 5:
        return {"status": "closed", "reason": "weekend", "display": "周末休市"}
    if hm < (9, 15):
        return {"status": "closed", "reason": "pre_market", "display": "未开盘"}
    if hm < (9, 25):
        return {"status": "auction", "reason": "call_auction", "display": "集合竞价"}
    if hm < (9, 30):
        return {"status": "auction", "reason": "pre_open", "display": "即将开盘"}
    if hm <= (11, 30):
        return {"status": "trading", "reason": "morning", "display": "上午交易中"}
    if hm < (13, 0):
        return {"status": "break", "reason": "lunch", "display": "午间休市"}
    if hm < (15, 0):
        return {"status": "trading", "reason": "afternoon", "display": "下午交易中"}
    return {"status": "closed", "reason": "after_hours", "display": "已收盘"}


# ---------------------------------------------------------------------------
# 股票搜索 (akshare — 数据来自交易所，稳定可靠)
# ---------------------------------------------------------------------------

def search_stock(keyword: str) -> list[dict]:
    import akshare as ak
    try:
        df = retry(lambda: ak.stock_info_a_code_name())
    except Exception:
        return [{"error": "股票列表获取失败，请稍后再试"}]

    if "code" not in df.columns:
        df.columns = ["code", "name"]

    mask = df["name"].str.contains(keyword, na=False) | df["code"].str.contains(keyword, na=False)
    results = df[mask].head(10)
    return [
        {"code": row["code"], "name": row["name"], "market": detect_market(row["code"])}
        for _, row in results.iterrows()
    ]


# ---------------------------------------------------------------------------
# 实时行情 (新浪财经 hq API — 单股轻量请求)
# ---------------------------------------------------------------------------

def fetch_realtime_quote(code: str, market: str) -> dict:
    symbol = f"{market}{code}"
    url = f"https://hq.sinajs.cn/list={symbol}"
    r = retry(lambda: requests.get(url, headers=SINA_HEADERS, timeout=10))
    text = r.text.strip()

    if '=""' in text or not text:
        return {}

    parts = text.split('"')[1].split(",")
    if len(parts) < 32:
        return {}

    pre_close = safe_float(parts[2])
    price = safe_float(parts[3])
    change_pct = round((price - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0.0

    return {
        "name": parts[0],
        "code": code,
        "price": price,
        "change_pct": change_pct,
        "open": safe_float(parts[1]),
        "pre_close": pre_close,
        "high": safe_float(parts[4]),
        "low": safe_float(parts[5]),
        "volume": safe_float(parts[8]),
        "amount": round(safe_float(parts[9]) / 10000, 2),
        "turnover_rate": 0.0,
        "date": parts[30] if len(parts) > 30 else "",
        "time": parts[31] if len(parts) > 31 else "",
    }


# ---------------------------------------------------------------------------
# 历史K线 (新浪财经 K线 API)
# ---------------------------------------------------------------------------

def fetch_daily_data(code: str, market: str, days: int = 120) -> pd.DataFrame:
    symbol = f"{market}{code}"
    url = "https://quotes.sina.cn/cn/api/jsonp.php/t/CN_MarketDataService.getKLineData"
    params = {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(days)}
    r = retry(lambda: requests.get(url, params=params, headers=SINA_HEADERS, timeout=15))

    text = r.text
    json_str = text[text.index("(") + 1 : text.rindex(")")]
    data = json.loads(json_str)

    rows = []
    for item in data:
        rows.append({
            "date": item["day"],
            "open": float(item["open"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "close": float(item["close"]),
            "volume": float(item["volume"]),
        })

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    if len(df) > 1:
        df["change_pct"] = df["close"].pct_change() * 100
    else:
        df["change_pct"] = 0.0

    return df


# ---------------------------------------------------------------------------
# 技术指标计算
# ---------------------------------------------------------------------------

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    for w in (5, 10, 20, 60, 120):
        df[f"ma{w}"] = c.rolling(w).mean()

    macd_ind = ta.trend.MACD(c, window_slow=26, window_fast=12, window_sign=9)
    df["macd"] = macd_ind.macd()
    df["macd_signal"] = macd_ind.macd_signal()
    df["macd_hist"] = macd_ind.macd_diff()

    stoch = ta.momentum.StochasticOscillator(h, l, c, window=9, smooth_window=3)
    df["k"] = stoch.stoch()
    df["d"] = stoch.stoch_signal()
    df["j"] = 3 * df["k"] - 2 * df["d"]

    for w in (6, 12, 24):
        df[f"rsi{w}"] = ta.momentum.RSIIndicator(c, window=w).rsi()

    bb = ta.volatility.BollingerBands(c, window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_width"] = bb.bollinger_wband()

    df["atr"] = ta.volatility.AverageTrueRange(h, l, c, window=14).average_true_range()

    for w in (5, 20):
        df[f"vol_ma{w}"] = v.rolling(w).mean()

    return df


# ---------------------------------------------------------------------------
# 信号检测
# ---------------------------------------------------------------------------

def detect_signals(df: pd.DataFrame) -> list[dict]:
    signals = []
    if len(df) < 3:
        return signals

    cur = df.iloc[-1]
    prev = df.iloc[-2]

    def add(name, direction, strength, detail):
        signals.append({"name": name, "direction": direction, "strength": strength, "detail": detail})

    # ── 均线交叉 ─────────────────────────────────────────────
    ma_pairs = [("ma5", "ma10", "MA5/MA10"), ("ma5", "ma20", "MA5/MA20"),
                ("ma10", "ma20", "MA10/MA20"), ("ma10", "ma60", "MA10/MA60")]
    for fast, slow, label in ma_pairs:
        if all(pd.notna(cur.get(k)) and pd.notna(prev.get(k)) for k in (fast, slow)):
            if prev[fast] <= prev[slow] and cur[fast] > cur[slow]:
                add(f"{label} 金叉", "bullish", "normal", f"{fast}={cur[fast]:.2f}, {slow}={cur[slow]:.2f}")
            elif prev[fast] >= prev[slow] and cur[fast] < cur[slow]:
                add(f"{label} 死叉", "bearish", "normal", f"{fast}={cur[fast]:.2f}, {slow}={cur[slow]:.2f}")

    if all(pd.notna(cur.get(f"ma{w}")) for w in (5, 10, 20, 60)):
        if cur["ma5"] > cur["ma10"] > cur["ma20"] > cur["ma60"]:
            add("均线多头排列", "bullish", "strong", "MA5>MA10>MA20>MA60，趋势向上")
        elif cur["ma5"] < cur["ma10"] < cur["ma20"] < cur["ma60"]:
            add("均线空头排列", "bearish", "strong", "MA5<MA10<MA20<MA60，趋势向下")

    # ── MACD ─────────────────────────────────────────────────
    if pd.notna(cur["macd"]) and pd.notna(prev["macd"]):
        if prev["macd"] <= prev["macd_signal"] and cur["macd"] > cur["macd_signal"]:
            loc = "零轴上方" if cur["macd"] > 0 else "零轴下方"
            add(f"MACD 金叉（{loc}）", "bullish", "strong" if cur["macd"] > 0 else "normal",
                f"DIF={cur['macd']:.4f} 上穿 DEA={cur['macd_signal']:.4f}")
        elif prev["macd"] >= prev["macd_signal"] and cur["macd"] < cur["macd_signal"]:
            loc = "零轴上方" if cur["macd"] > 0 else "零轴下方"
            add(f"MACD 死叉（{loc}）", "bearish", "strong" if cur["macd"] < 0 else "normal",
                f"DIF={cur['macd']:.4f} 下穿 DEA={cur['macd_signal']:.4f}")

        if prev["macd_hist"] < 0 and cur["macd_hist"] > 0:
            add("MACD 红柱出现", "bullish", "normal", "柱状体由负转正，动能翻多")
        elif prev["macd_hist"] > 0 and cur["macd_hist"] < 0:
            add("MACD 绿柱出现", "bearish", "normal", "柱状体由正转负，动能翻空")

    # MACD 背离
    lookback = min(30, len(df) - 1)
    recent = df.tail(lookback)
    if len(recent) >= 10:
        if recent["close"].idxmax() == recent.index[-1]:
            macd_max_idx = recent["macd"].idxmax()
            if macd_max_idx != recent.index[-1] and cur["macd"] < recent.loc[macd_max_idx, "macd"]:
                add("MACD 顶背离（疑似）", "bearish", "strong", "价格创新高但MACD未跟随，注意回调风险")
        if recent["close"].idxmin() == recent.index[-1]:
            macd_min_idx = recent["macd"].idxmin()
            if macd_min_idx != recent.index[-1] and cur["macd"] > recent.loc[macd_min_idx, "macd"]:
                add("MACD 底背离（疑似）", "bullish", "strong", "价格创新低但MACD未跟随，留意反弹")

    # ── KDJ ──────────────────────────────────────────────────
    if pd.notna(cur.get("k")) and pd.notna(cur.get("d")):
        if prev["k"] <= prev["d"] and cur["k"] > cur["d"]:
            zone = "超卖区" if cur["k"] < 20 else "中位"
            add(f"KDJ 金叉（{zone}）", "bullish", "strong" if cur["k"] < 20 else "normal",
                f"K={cur['k']:.1f}, D={cur['d']:.1f}, J={cur['j']:.1f}")
        elif prev["k"] >= prev["d"] and cur["k"] < cur["d"]:
            zone = "超买区" if cur["k"] > 80 else "中位"
            add(f"KDJ 死叉（{zone}）", "bearish", "strong" if cur["k"] > 80 else "normal",
                f"K={cur['k']:.1f}, D={cur['d']:.1f}, J={cur['j']:.1f}")
        if cur["j"] > 100:
            add("KDJ J值超买", "bearish", "normal", f"J={cur['j']:.1f}>100，短期过热")
        elif cur["j"] < 0:
            add("KDJ J值超卖", "bullish", "normal", f"J={cur['j']:.1f}<0，短期超跌")

    # ── RSI ──────────────────────────────────────────────────
    for w in (6, 12, 24):
        col = f"rsi{w}"
        if pd.notna(cur.get(col)):
            if cur[col] > 80:
                add(f"RSI{w} 超买", "bearish", "normal", f"RSI{w}={cur[col]:.1f}>80")
            elif cur[col] < 20:
                add(f"RSI{w} 超卖", "bullish", "normal", f"RSI{w}={cur[col]:.1f}<20")

    if pd.notna(cur.get("rsi6")) and pd.notna(cur.get("rsi12")):
        if prev["rsi6"] <= prev["rsi12"] and cur["rsi6"] > cur["rsi12"]:
            add("RSI6/RSI12 金叉", "bullish", "normal", f"RSI6={cur['rsi6']:.1f} 上穿 RSI12={cur['rsi12']:.1f}")
        elif prev["rsi6"] >= prev["rsi12"] and cur["rsi6"] < cur["rsi12"]:
            add("RSI6/RSI12 死叉", "bearish", "normal", f"RSI6={cur['rsi6']:.1f} 下穿 RSI12={cur['rsi12']:.1f}")

    # ── 布林带 ───────────────────────────────────────────────
    if pd.notna(cur.get("bb_upper")) and pd.notna(cur.get("bb_lower")):
        if cur["close"] > cur["bb_upper"]:
            add("突破布林上轨", "bearish", "normal", f"收盘{cur['close']:.2f} > 上轨{cur['bb_upper']:.2f}")
        elif cur["close"] < cur["bb_lower"]:
            add("跌破布林下轨", "bullish", "normal", f"收盘{cur['close']:.2f} < 下轨{cur['bb_lower']:.2f}")
        if pd.notna(prev.get("bb_width")) and cur["bb_width"] < 0.05 and prev["bb_width"] < 0.05:
            add("布林带极度收口", "neutral", "normal", f"带宽={cur['bb_width']:.4f}，变盘信号")

    # ── 成交量异常 ───────────────────────────────────────────
    if pd.notna(cur.get("vol_ma5")) and cur["vol_ma5"] > 0:
        vol_ratio = cur["volume"] / cur["vol_ma5"]
        if vol_ratio > 3:
            add("巨量异动", "neutral", "strong", f"成交量是5日均量的{vol_ratio:.1f}倍")
        elif vol_ratio > 2:
            d = "bullish" if cur["change_pct"] > 0 else "bearish"
            tag = "放量上涨" if cur["change_pct"] > 0 else "放量下跌"
            add(tag, d, "normal", f"量比{vol_ratio:.1f}倍 | 涨跌幅{cur['change_pct']:.2f}%")
        elif vol_ratio < 0.3:
            add("极度缩量", "neutral", "normal", f"量比仅{vol_ratio:.1f}倍，市场观望")

    # ── K线形态 ──────────────────────────────────────────────
    body = abs(cur["close"] - cur["open"])
    upper_shadow = cur["high"] - max(cur["close"], cur["open"])
    lower_shadow = min(cur["close"], cur["open"]) - cur["low"]
    total_range = cur["high"] - cur["low"]

    if total_range > 0:
        if body / total_range < 0.1:
            add("十字星", "neutral", "normal", f"实体占比{body/total_range:.1%}，可能变盘")
        if body > 0:
            if lower_shadow > body * 2 and upper_shadow < body * 0.5:
                add("锤子线", "bullish", "normal", f"长下影线{lower_shadow:.2f}，下方有支撑")
            if upper_shadow > body * 2 and lower_shadow < body * 0.5:
                add("射击之星", "bearish", "normal", f"长上影线{upper_shadow:.2f}，上方有压力")

    if cur["close"] > cur["open"] and prev["close"] < prev["open"]:
        if cur["close"] > prev["open"] and cur["open"] < prev["close"]:
            add("看涨吞没", "bullish", "strong", "阳线完全包裹前日阴线，反转信号")
    if cur["close"] < cur["open"] and prev["close"] > prev["open"]:
        if cur["open"] > prev["close"] and cur["close"] < prev["open"]:
            add("看跌吞没", "bearish", "strong", "阴线完全包裹前日阳线，反转信号")

    # ── 涨跌幅预警 ───────────────────────────────────────────
    pct = abs(cur["change_pct"]) if pd.notna(cur.get("change_pct")) else 0
    if pct >= 7:
        d = "bullish" if cur["change_pct"] > 0 else "bearish"
        add("大幅波动预警", d, "strong", f"涨跌幅{cur['change_pct']:.2f}%")
    elif pct >= 5:
        d = "bullish" if cur["change_pct"] > 0 else "bearish"
        add("显著波动", d, "normal", f"涨跌幅{cur['change_pct']:.2f}%")

    # ── 关键均线突破/跌破 ────────────────────────────────────
    for w in (20, 60, 120):
        ma_col = f"ma{w}"
        if pd.notna(cur.get(ma_col)) and pd.notna(prev.get(ma_col)) and cur[ma_col] > 0:
            if cur["close"] > cur[ma_col] and prev["close"] < prev[ma_col]:
                add(f"站上 MA{w}", "bullish", "normal", f"突破{w}日均线{cur[ma_col]:.2f}")
            elif cur["close"] < cur[ma_col] and prev["close"] > prev[ma_col]:
                add(f"跌破 MA{w}", "bearish", "normal", f"跌破{w}日均线{cur[ma_col]:.2f}")

    # ── TD Setup 9（参考 glod 策略）──────────────────────────
    if len(df) >= 13:
        buy_cnt = 0
        sell_cnt = 0
        for i in range(len(df)):
            if i >= 4 and df.iloc[i]["close"] < df.iloc[i - 4]["close"]:
                buy_cnt += 1
            else:
                buy_cnt = 0
            if i >= 4 and df.iloc[i]["close"] > df.iloc[i - 4]["close"]:
                sell_cnt += 1
            else:
                sell_cnt = 0

        # 只在第9根触发，避免连续重复提醒
        if buy_cnt == 9:
            ret20 = None
            if len(df) >= 21 and df.iloc[-21]["close"] > 0:
                ret20 = cur["close"] / df.iloc[-21]["close"] - 1
            perfected = False
            if len(df) >= 10:
                low6 = df.iloc[-4]["low"]
                low7 = df.iloc[-3]["low"]
                low8 = df.iloc[-2]["low"]
                low9 = df.iloc[-1]["low"]
                perfected = min(low8, low9) < min(low6, low7)

            if ret20 is not None and ret20 <= 0:
                tag = "TD Buy 9（回撤过滤）"
                strength = "strong"
            else:
                tag = "TD Buy 9"
                strength = "normal"
            if perfected:
                tag += " + Perfected"
                strength = "strong"
            detail = f"连续9根收盘低于4日前收盘；20日收益={ret20:.2%}" if ret20 is not None else "连续9根收盘低于4日前收盘"
            add(tag, "bullish", strength, detail)

        if sell_cnt == 9:
            ret20 = None
            if len(df) >= 21 and df.iloc[-21]["close"] > 0:
                ret20 = cur["close"] / df.iloc[-21]["close"] - 1
            perfected = False
            if len(df) >= 10:
                high6 = df.iloc[-4]["high"]
                high7 = df.iloc[-3]["high"]
                high8 = df.iloc[-2]["high"]
                high9 = df.iloc[-1]["high"]
                perfected = max(high8, high9) > max(high6, high7)

            if ret20 is not None and ret20 >= 0.07:
                tag = "TD Sell 9（过热过滤）"
                strength = "strong"
            else:
                tag = "TD Sell 9"
                strength = "normal"
            if perfected:
                tag += " + Perfected"
                strength = "strong"
            detail = f"连续9根收盘高于4日前收盘；20日收益={ret20:.2%}" if ret20 is not None else "连续9根收盘高于4日前收盘"
            add(tag, "bearish", strength, detail)

    return signals


# ---------------------------------------------------------------------------
# 关键价位 & 汇总
# ---------------------------------------------------------------------------

def compute_key_levels(df: pd.DataFrame) -> dict:
    cur = df.iloc[-1]
    recent = df.tail(30)
    resistance, support = [], []

    if pd.notna(cur.get("bb_upper")):
        resistance.append(cur["bb_upper"])
    if pd.notna(cur.get("bb_lower")):
        support.append(cur["bb_lower"])
    for w in (20, 60):
        col = f"ma{w}"
        if pd.notna(cur.get(col)):
            (resistance if cur[col] > cur["close"] else support).append(cur[col])

    rh, rl = recent["high"].max(), recent["low"].min()
    if rh > cur["close"]:
        resistance.append(rh)
    if rl < cur["close"]:
        support.append(rl)

    return {
        "resistance": round(float(min(resistance)), 2) if resistance else round(float(cur["high"]), 2),
        "support": round(float(max(support)), 2) if support else round(float(cur["low"]), 2),
    }


def summarize_signals(signals: list[dict]) -> dict:
    bull = sum(1 for s in signals if s["direction"] == "bullish")
    bear = sum(1 for s in signals if s["direction"] == "bearish")
    neut = sum(1 for s in signals if s["direction"] == "neutral")
    if bull > bear + 2:
        bias = "bullish"
    elif bear > bull + 2:
        bias = "bearish"
    else:
        bias = "neutral"
    return {"bullish_count": bull, "bearish_count": bear, "neutral_count": neut, "bias": bias}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_analyze(code: str, days: int = 120):
    market = detect_market(code)

    try:
        quote = fetch_realtime_quote(code, market)
    except Exception as e:
        quote = {"name": code, "code": code, "error": str(e)}

    if not quote:
        return json.dumps({"error": f"未找到股票 {code}，请检查代码"}, ensure_ascii=False)

    try:
        df = fetch_daily_data(code, market, days)
    except Exception as e:
        return json.dumps({"error": f"历史数据获取失败: {e}"}, ensure_ascii=False)

    if df.empty:
        return json.dumps({"error": f"无法获取 {code} 的历史数据"}, ensure_ascii=False)

    df = compute_indicators(df)
    signals = detect_signals(df)
    key_levels = compute_key_levels(df)
    summary = summarize_signals(signals)

    cur = df.iloc[-1]
    indicators = {}
    for col in ("macd", "macd_signal", "macd_hist", "k", "d", "j",
                "rsi6", "rsi12", "rsi24", "bb_upper", "bb_mid", "bb_lower", "bb_width", "atr"):
        val = cur.get(col)
        indicators[col] = round(float(val), 4) if pd.notna(val) else None
    for w in (5, 10, 20, 60, 120):
        val = cur.get(f"ma{w}")
        indicators[f"ma{w}"] = round(float(val), 4) if pd.notna(val) else None

    if quote.get("price", 0) == 0:
        quote.update({"price": float(cur["close"]), "high": float(cur["high"]),
                       "low": float(cur["low"]), "open": float(cur["open"]),
                       "volume": float(cur["volume"])})

    return json.dumps({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_date": str(cur["date"].date()) if pd.notna(cur.get("date")) else "",
        "market_status": get_market_status(),
        "quote": quote,
        "indicators": indicators,
        "signals": signals,
        "key_levels": key_levels,
        "summary": summary,
    }, ensure_ascii=False, indent=2)


def run_search(keyword: str):
    return json.dumps(search_stock(keyword), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 持仓与策略（参照 openclaw-portfolio-skill：持仓占比 + 策略建议）
# ---------------------------------------------------------------------------

# 策略默认参数（与 strategy/a_share_simple.md 一致）
STRATEGY_MAX_SINGLE_WEIGHT = 0.30
STRATEGY_MIN_CASH_WEIGHT = 0.05
STRATEGY_TOP3_CONCENTRATION_LIMIT = 0.70
STRATEGY_REBALANCE_THRESHOLD = 0.05


def _extract_yaml_block(text: str, key_hint: str) -> str | None:
    """从 Markdown 中提取包含 key_hint 的 ```yaml ... ``` 块内容。"""
    fence = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)
    for m in fence.finditer(text):
        if key_hint in m.group(1):
            return m.group(1).strip()
    return None


def _extract_yaml_blocks(text: str) -> list[str]:
    fence = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)
    return [m.group(1).strip() for m in fence.finditer(text)]


def parse_position_file(path: str, default_cash: float = 0.0) -> tuple[list[dict], float]:
    """
    解析持仓文件（current_position.md 或 CSV）。
    返回 (positions, cash_cny)。
    positions: [{"code": "600519", "name": "贵州茅台", "shares": 100, "bucket": "core"}, ...]
    """
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return [], default_cash

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    # 尝试 CSV（东方财富/同花顺 导出常见列名）
    if path.lower().endswith(".csv"):
        return _parse_position_csv(raw, default_cash)

    # Markdown + YAML
    cash_cny = default_cash
    positions = []

    cash_block = _extract_yaml_block(raw, "cash_cny")
    if cash_block and yaml:
        try:
            data = yaml.safe_load(cash_block)
            if isinstance(data, dict) and "cash_cny" in data:
                cash_cny = float(data["cash_cny"])
        except Exception:
            pass

    pos_block = _extract_yaml_block(raw, "positions")
    if pos_block and yaml:
        try:
            data = yaml.safe_load(pos_block)
            if isinstance(data, dict) and "positions" in data:
                for p in data["positions"]:
                    if not isinstance(p, dict) or not p.get("code"):
                        continue
                    code = str(p.get("code", "")).strip()
                    if not code or len(code) < 5:
                        continue
                    try:
                        shares = float(p.get("shares", 0))
                    except (TypeError, ValueError):
                        shares = 0
                    if shares <= 0:
                        continue
                    positions.append({
                        "code": code,
                        "name": str(p.get("name") or code),
                        "shares": shares,
                        "bucket": str(p.get("bucket") or "default"),
                    })
        except Exception:
            pass

    return positions, cash_cny


def parse_strategy_config_from_position_file(path: str) -> dict:
    """
    从持仓 markdown 中解析可选策略配置（兼容多 YAML 代码块）。
    支持字段：
      - max_single_weight
      - min_cash_weight
      - top3_concentration_limit
      - rebalance_threshold
      - asset_bucket_limits: {bucket: {min: 0.1, max: 0.4}}
    """
    path = os.path.abspath(path)
    if not os.path.exists(path) or path.lower().endswith(".csv") or not yaml:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception:
        return {}

    merged = {}
    for blk in _extract_yaml_blocks(raw):
        try:
            data = yaml.safe_load(blk)
            if isinstance(data, dict):
                merged.update(data)
        except Exception:
            continue

    cfg = {}
    for k in ("max_single_weight", "min_cash_weight", "top3_concentration_limit", "rebalance_threshold"):
        v = merged.get(k)
        if v is None:
            continue
        try:
            cfg[k] = float(v)
        except (TypeError, ValueError):
            pass

    abl = merged.get("asset_bucket_limits")
    if isinstance(abl, dict):
        normalized = {}
        for bk, lim in abl.items():
            if not isinstance(lim, dict):
                continue
            item = {}
            if "min" in lim:
                try:
                    item["min"] = float(lim["min"])
                except (TypeError, ValueError):
                    pass
            if "max" in lim:
                try:
                    item["max"] = float(lim["max"])
                except (TypeError, ValueError):
                    pass
            if item:
                normalized[str(bk)] = item
        if normalized:
            cfg["asset_bucket_limits"] = normalized
    return cfg


def run_portfolio_init(position_file: str, force: bool = False) -> str:
    """初始化持仓文件模板，便于用户后续持续维护。"""
    position_file = os.path.abspath(position_file)
    if os.path.exists(position_file) and not force:
        return f"目标文件已存在：`{position_file}`。如需覆盖请加 `--force`。"
    tpl = (
        "# 当前持仓\n\n"
        "```yaml\n"
        "cash_cny: 80000\n"
        "```\n\n"
        "```yaml\n"
        "positions:\n"
        "  - code: \"600519\"\n"
        "    name: \"贵州茅台\"\n"
        "    shares: 100\n"
        "    bucket: \"core\"\n"
        "  - code: \"512890\"\n"
        "    name: \"红利低波ETF\"\n"
        "    shares: 3000\n"
        "    bucket: \"dividend\"\n"
        "```\n\n"
        "```yaml\n"
        "# 可选策略覆盖（不填则走默认）\n"
        "max_single_weight: 0.30\n"
        "min_cash_weight: 0.05\n"
        "top3_concentration_limit: 0.70\n"
        "rebalance_threshold: 0.05\n"
        "asset_bucket_limits:\n"
        "  core: {min: 0.20, max: 0.60}\n"
        "  dividend: {min: 0.10, max: 0.40}\n"
        "  gold: {max: 0.20}\n"
        "```\n"
    )
    with open(position_file, "w", encoding="utf-8") as f:
        f.write(tpl)
    return f"已初始化持仓模板：`{position_file}`"


def run_portfolio_strategy(position_file: str = "current_position.md") -> str:
    """
    展示默认策略、文件覆盖策略和最终生效策略。
    """
    defaults = {
        "max_single_weight": STRATEGY_MAX_SINGLE_WEIGHT,
        "min_cash_weight": STRATEGY_MIN_CASH_WEIGHT,
        "top3_concentration_limit": STRATEGY_TOP3_CONCENTRATION_LIMIT,
        "rebalance_threshold": STRATEGY_REBALANCE_THRESHOLD,
    }
    cfg = parse_strategy_config_from_position_file(position_file) if position_file else {}
    effective = {
        "max_single_weight": _safe_num(cfg.get("max_single_weight"), defaults["max_single_weight"]),
        "min_cash_weight": _safe_num(cfg.get("min_cash_weight"), defaults["min_cash_weight"]),
        "top3_concentration_limit": _safe_num(
            cfg.get("top3_concentration_limit"),
            defaults["top3_concentration_limit"],
        ),
        "rebalance_threshold": _safe_num(cfg.get("rebalance_threshold"), defaults["rebalance_threshold"]),
    }
    bucket_limits = cfg.get("asset_bucket_limits", {}) if isinstance(cfg, dict) else {}

    lines = [
        "# 策略说明（自然语言）",
        "",
        f"- 持仓文件：`{os.path.abspath(position_file)}`",
        "",
        "## 默认策略（系统内置）",
        f"- 单只股票通常不建议超过总资产的 **{defaults['max_single_weight']:.0%}**，避免个股风险过度集中。",
        f"- 现金通常建议至少保留 **{defaults['min_cash_weight']:.0%}**，用于应对波动和后续调仓。",
        f"- 前三大持仓合计通常不建议超过 **{defaults['top3_concentration_limit']:.0%}**，保持组合分散。",
        f"- 当某项仓位偏离目标约 **{defaults['rebalance_threshold']:.0%}** 时，系统会提示你考虑再平衡。",
        "",
        "## 你在文件里写的策略覆盖",
    ]
    if cfg:
        for k in ("max_single_weight", "min_cash_weight", "top3_concentration_limit", "rebalance_threshold"):
            if k in cfg:
                lines.append(f"- `{k}` = {cfg[k]}")
    else:
        lines.append("- 你还没有写覆盖项，当前使用默认策略。")

    lines.extend([
        "",
        "## 当前实际生效策略",
        f"- 单票上限：**{effective['max_single_weight']:.0%}**",
        f"- 现金下限：**{effective['min_cash_weight']:.0%}**",
        f"- 前三集中度上限：**{effective['top3_concentration_limit']:.0%}**",
        f"- 再平衡阈值：**{effective['rebalance_threshold']:.0%}**",
    ])
    if bucket_limits:
        lines.append("- 资产桶约束：")
        for bk, lim in bucket_limits.items():
            parts = []
            if "min" in lim:
                parts.append(f"最低 {float(lim['min']):.0%}")
            if "max" in lim:
                parts.append(f"最高 {float(lim['max']):.0%}")
            lines.append(f"  - `{bk}`：{', '.join(parts)}")
    return "\n".join(lines)


def _parse_cookie_header(cookie_str: str) -> dict:
    cookies = {}
    for part in (cookie_str or "").split(";"):
        seg = part.strip()
        if not seg or "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        k = k.strip()
        if k:
            cookies[k] = v.strip()
    return cookies


def _load_cookie_text(cookie_text: str | None, cookie_file: str | None) -> str:
    if cookie_text:
        return cookie_text.strip()
    if cookie_file:
        with open(cookie_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def _extract_validatekey(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"(?:\?|&)validatekey=([^&]+)", text)
    return m.group(1) if m else None


def _extract_validatekey_from_post_data(post_data: str | None) -> str | None:
    if not post_data:
        return None
    direct = _extract_validatekey(post_data)
    if direct:
        return direct
    try:
        qs = parse_qs(post_data, keep_blank_values=True)
        vals = qs.get("validatekey")
        if vals and vals[0]:
            return vals[0]
    except Exception:
        pass
    try:
        obj = json.loads(post_data)
        if isinstance(obj, dict):
            v = obj.get("validatekey")
            if v:
                return str(v)
    except Exception:
        pass
    return None


def _cookie_list_to_header(cookies: list[dict]) -> str:
    pairs = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        domain = str(c.get("domain") or "")
        if "18.cn" not in domain:
            continue
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "")
        if not name:
            continue
        pairs.append(f"{name}={value}")
    # 去重，后出现的覆盖前面的
    seen = {}
    for p in pairs:
        k = p.split("=", 1)[0]
        seen[k] = p
    return "; ".join(seen.values())


def _load_session_payload(session_file: str | None) -> dict:
    if not session_file:
        return {}
    if not os.path.exists(session_file):
        return {}
    try:
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve_eastmoney_auth(
    eastmoney_cookie: str | None,
    eastmoney_cookie_file: str | None,
    eastmoney_validatekey: str | None,
    eastmoney_session_file: str | None,
) -> tuple[str, str | None, str]:
    """
    返回 (cookie_text, validatekey, source)
    source: cli_cookie | cli_cookie_file | default_cookie_file | session_file | none
    """
    if eastmoney_cookie and eastmoney_cookie.strip():
        return eastmoney_cookie.strip(), eastmoney_validatekey, "cli_cookie"

    if eastmoney_cookie_file:
        cookie_text = _load_cookie_text(None, eastmoney_cookie_file)
        if cookie_text:
            return cookie_text, eastmoney_validatekey, "cli_cookie_file"

    if os.path.exists(DEFAULT_EASTMONEY_COOKIE_FILE):
        cookie_text = _load_cookie_text(None, DEFAULT_EASTMONEY_COOKIE_FILE)
        if cookie_text:
            return cookie_text, eastmoney_validatekey, "default_cookie_file"

    session_path = eastmoney_session_file or DEFAULT_EASTMONEY_SESSION_FILE
    payload = _load_session_payload(session_path)
    if payload:
        cookie_text = str(payload.get("cookie") or "").strip()
        vk = eastmoney_validatekey or payload.get("validatekey")
        if cookie_text:
            return cookie_text, (str(vk).strip() if vk else None), "session_file"

    return "", eastmoney_validatekey, "none"


def run_eastmoney_login(
    session_file: str = DEFAULT_EASTMONEY_SESSION_FILE,
    cookie_file: str = DEFAULT_EASTMONEY_COOKIE_FILE,
    login_url: str = "https://jywg.18.cn/",
    timeout_sec: int = 180,
    headless: bool = False,
    auto_test: bool = False,
) -> str:
    """
    打开浏览器让用户手动登录东方财富，自动提取 cookie/validatekey 并保存到本地。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return (
            "**缺少 Playwright 依赖。**\n\n"
            "请先执行：\n"
            "1. `python -m pip install playwright`\n"
            "2. `python -m playwright install chromium`\n"
            "完成后重试 `eastmoney login`。"
        )

    timeout_sec = max(30, int(timeout_sec))
    cookie_text = ""
    detected_validatekey = None
    err_msg = ""
    has_positions = False
    network_hits = 0
    vk_candidates = []
    vk_from_trade_endpoint = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()

            def _on_request(req):
                nonlocal detected_validatekey, network_hits, vk_from_trade_endpoint
                url = req.url or ""
                if "jywg.18.cn" not in url:
                    return
                network_hits += 1
                hit_trade = "/Com/GetAssetAndPosition" in url or "/Com/GetPosition" in url

                vk = _extract_validatekey(url)
                if not vk:
                    vk = _extract_validatekey_from_post_data(req.post_data)
                if vk:
                    vk_candidates.append(vk)
                    if hit_trade:
                        vk_from_trade_endpoint = vk
                        detected_validatekey = vk
                    elif detected_validatekey is None:
                        detected_validatekey = vk

            def _on_response(resp):
                nonlocal detected_validatekey, vk_from_trade_endpoint
                url = resp.url or ""
                if "jywg.18.cn" not in url:
                    return
                hit_trade = "/Com/GetAssetAndPosition" in url or "/Com/GetPosition" in url
                vk = _extract_validatekey(url)
                if vk:
                    vk_candidates.append(vk)
                    if hit_trade:
                        vk_from_trade_endpoint = vk
                        detected_validatekey = vk
                    elif detected_validatekey is None:
                        detected_validatekey = vk

            page.on("request", _on_request)
            page.on("response", _on_response)
            page.goto(login_url, wait_until="domcontentloaded", timeout=60000)

            print(
                f"[eastmoney-login] 浏览器已打开，请在 {timeout_sec}s 内完成登录。",
                file=sys.stderr,
            )

            start_ts = time.time()
            while time.time() - start_ts < timeout_sec:
                cur_url = page.url
                cur_vk = _extract_validatekey(cur_url)
                if cur_vk:
                    detected_validatekey = cur_vk

                cookies = context.cookies()
                cookie_text = _cookie_list_to_header(cookies)
                if cookie_text:
                    # 交易接口抓到的 validatekey 可信度最高
                    if vk_from_trade_endpoint:
                        detected_validatekey = vk_from_trade_endpoint
                    try:
                        positions, _ = fetch_positions_from_eastmoney(
                            cookie_text=cookie_text,
                            validatekey=detected_validatekey,
                        )
                        if positions:
                            has_positions = True
                            break
                    except Exception as e:
                        err_msg = str(e)
                time.sleep(2)

            browser.close()
    except Exception as e:
        return f"**东方财富登录自动化失败**：{e}"

    if not cookie_text:
        return (
            "**未捕获到有效 cookie。**\n\n"
            "请确认已在弹出的浏览器里完成登录，再重试 `eastmoney login`。"
        )

    os.makedirs(os.path.dirname(os.path.abspath(session_file)) or ".", exist_ok=True)
    session_payload = {
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cookie": cookie_text,
        "validatekey": detected_validatekey or "",
        "login_url": login_url,
    }
    with open(session_file, "w", encoding="utf-8") as f:
        json.dump(session_payload, f, ensure_ascii=False, indent=2)
    with open(cookie_file, "w", encoding="utf-8") as f:
        f.write(cookie_text)

    lines = [
        "## 东方财富登录态已保存",
        "",
        f"- session 文件：`{session_file}`",
        f"- cookie 文件：`{cookie_file}`",
        f"- validatekey：`{detected_validatekey or '未检测到（可选）'}`",
        f"- 监听到东财网络请求数：`{network_hits}`",
    ]
    if vk_candidates:
        uniq_vks = list(dict.fromkeys(vk_candidates))
        show_vks = uniq_vks[:3]
        lines.append(f"- 捕获到 validatekey 候选：`{', '.join(show_vks)}`")
        if len(uniq_vks) > 3:
            lines.append(f"- 其余候选数量：`{len(uniq_vks) - 3}`")
    if has_positions:
        lines.append("- 状态：已验证可读取持仓 ✅")
    else:
        lines.append("- 状态：已保存登录态，但暂未验证到持仓（可能需要 validatekey 或重新登录）")
        if err_msg:
            lines.append(f"- 最近错误：`{err_msg}`")
    lines.extend([
        "",
        "下一步可直接执行：",
        "`python scripts/stock_monitor.py portfolio snapshot --provider auto --out snapshot.json`",
    ])
    if auto_test:
        lines.extend(["", "## 自动测试结果"])
        lines.append(run_eastmoney_test(
            eastmoney_cookie=cookie_text,
            eastmoney_validatekey=detected_validatekey,
        ))
    return "\n".join(lines)


def run_eastmoney_test(
    eastmoney_cookie: str | None = None,
    eastmoney_cookie_file: str | None = None,
    eastmoney_validatekey: str | None = None,
    eastmoney_session_file: str | None = None,
) -> str:
    cookie_text, validatekey, source = resolve_eastmoney_auth(
        eastmoney_cookie=eastmoney_cookie,
        eastmoney_cookie_file=eastmoney_cookie_file,
        eastmoney_validatekey=eastmoney_validatekey,
        eastmoney_session_file=eastmoney_session_file,
    )
    if not cookie_text:
        return (
            "**未找到可用登录态。**\n\n"
            "请先执行 `python scripts/stock_monitor.py eastmoney login`，"
            "或手动传入 `--eastmoney-cookie` / `--eastmoney-cookie-file`。"
        )

    try:
        positions, cash_cny = fetch_positions_from_eastmoney(
            cookie_text=cookie_text,
            validatekey=validatekey,
        )
    except Exception as e:
        return (
            f"**东方财富登录态测试失败**：{e}\n\n"
            "建议重新执行 `eastmoney login` 刷新会话。"
        )

    sample = positions[:5]
    lines = [
        "## 东方财富登录态测试成功",
        "",
        f"- 来源：`{source}`",
        f"- 持仓条数：`{len(positions)}`",
        f"- 可用资金(估计)：`{cash_cny:,.2f}`",
        "- 持仓样例（最多5条）：",
    ]
    for p in sample:
        lines.append(f"  - {p['code']} {p['name']} | 数量 {p['shares']:.0f}")
    return "\n".join(lines)


def _safe_num(v, default=0.0):
    if v is None:
        return default
    if isinstance(v, str):
        v = v.replace(",", "").strip()
        if v in ("", "--", "null", "None"):
            return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _extract_from_obj(obj: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return default


def _extract_position_rows(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    candidates = (
        "Data", "data", "Datas", "datas", "Rows", "rows",
        "Result", "result", "Position", "position", "positions",
    )
    for key in candidates:
        v = payload.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for inner_key in ("Rows", "rows", "Data", "data", "positions"):
                inner = v.get(inner_key)
                if isinstance(inner, list):
                    return inner
    return []


def _extract_cash_from_payload(payload, default_cash: float = 0.0) -> float:
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            return _extract_cash_from_payload(payload[0], default_cash)
        return default_cash
    if not isinstance(payload, dict):
        return default_cash

    for block_key in ("Data", "data", "Result", "result"):
        block = payload.get(block_key)
        if isinstance(block, dict):
            val = _extract_from_obj(block, ("Kyzj", "cash", "Cash", "available_cash", "可用资金"))
            if val is not None:
                return _safe_num(val, default_cash)
        elif isinstance(block, list) and block and isinstance(block[0], dict):
            val = _extract_from_obj(block[0], ("Kyzj", "cash", "Cash", "available_cash", "可用资金"))
            if val is not None:
                return _safe_num(val, default_cash)

    val = _extract_from_obj(payload, ("Kyzj", "cash", "Cash", "available_cash", "可用资金"))
    if val is not None:
        return _safe_num(val, default_cash)
    return default_cash


def fetch_positions_from_eastmoney(
    cookie_text: str,
    validatekey: str | None = None,
) -> tuple[list[dict], float]:
    """
    使用东方财富登录态(cookie + validatekey)拉取持仓。
    注意：接口字段可能随东方财富调整，此处做了多字段兼容与容错。
    """
    if not cookie_text:
        raise ValueError("缺少东方财富 cookie")

    cookies = _parse_cookie_header(cookie_text)
    if not cookies:
        raise ValueError("东方财富 cookie 格式无效")

    session = requests.Session()
    session.headers.update(EASTMONEY_HEADERS)
    session.cookies.update(cookies)

    params = {}
    if validatekey:
        params["validatekey"] = validatekey

    # 常见的在线交易接口（不同券商席位或版本可能字段差异）
    endpoint_candidates = [
        "https://jywg.18.cn/Com/GetAssetAndPositionV2",
        "https://jywg.18.cn/Com/GetPositions",
        "https://jywg.18.cn/Com/GetPosition",
    ]

    payload = None
    last_err = None
    for url in endpoint_candidates:
        try:
            r = session.get(url, params=params, timeout=12)
            if r.status_code != 200:
                continue
            maybe_json = None
            try:
                maybe_json = r.json()
            except Exception:
                txt = r.text.strip()
                if txt.startswith("{") and txt.endswith("}"):
                    maybe_json = json.loads(txt)
            if maybe_json is None:
                continue
            rows = _extract_position_rows(maybe_json)
            if rows:
                payload = maybe_json
                break
        except Exception as e:
            last_err = e

    if payload is None:
        if last_err:
            raise RuntimeError(f"东方财富持仓接口请求失败: {last_err}")
        raise RuntimeError("无法从东方财富接口读取持仓，请检查 cookie / validatekey 是否有效")

    positions = []
    for row in _extract_position_rows(payload):
        if not isinstance(row, dict):
            continue
        code = str(_extract_from_obj(
            row,
            ("Zqdm", "zqdm", "Code", "code", "stock_code", "证券代码"),
            ""
        )).strip()
        if not code:
            continue
        code = code.replace("SH", "").replace("SZ", "").replace("BJ", "").replace(".", "")
        code = re.sub(r"[^0-9]", "", code) or code

        shares = _safe_num(_extract_from_obj(
            row,
            ("Zqsl", "zqsl", "CurrentAmount", "Qty", "shares", "持仓数量", "股票余额"),
            0
        ))
        if shares <= 0:
            continue

        price = _safe_num(_extract_from_obj(
            row,
            ("Zxjg", "zxjg", "LastPrice", "price", "最新价"),
            0
        ))
        market_value = _safe_num(_extract_from_obj(
            row,
            ("Zxsz", "zxsz", "MarketValue", "market_value", "市值"),
            0
        ))
        name = str(_extract_from_obj(
            row,
            ("Zqmc", "zqmc", "Name", "name", "证券名称", "股票名称"),
            code
        )).strip()
        if market_value <= 0 and price > 0:
            market_value = shares * price

        positions.append({
            "code": code,
            "name": name or code,
            "shares": shares,
            "bucket": "default",
            "price": price,
            "market_value": market_value,
        })

    if not positions:
        raise RuntimeError("东方财富接口返回成功，但未解析到有效持仓")

    cash_cny = _extract_cash_from_payload(payload, default_cash=0.0)
    return positions, cash_cny


def _parse_position_csv(raw: str, default_cash: float) -> tuple[list[dict], float]:
    """解析 CSV：支持 代码/股票代码、名称/股票名称、数量/持仓数量/股数 等列名。"""
    import csv
    import io
    positions = []
    cash_cny = default_cash
    try:
        reader = csv.DictReader(io.StringIO(raw.strip()))
        rows = list(reader)
        if not rows:
            return [], cash_cny
        first = rows[0]
        # 找代码列
        code_col = None
        for k in first:
            kk = (k or "").strip().lower()
            if "代码" in kk or kk == "code" or "symbol" in kk:
                code_col = k
                break
        if not code_col:
            return [], cash_cny
        name_col = None
        for k in first:
            kk = (k or "").strip().lower()
            if "名称" in kk or kk == "name" or "股票" in kk:
                name_col = k
                break
        share_col = None
        for k in first:
            kk = (k or "").strip().lower()
            if "数量" in kk or "股数" in kk or "持仓" in kk or kk in ("shares", "volume"):
                share_col = k
                break
        for row in rows:
            code = (row.get(code_col) or "").strip().replace("'", "")
            if not code or len(code) < 5:
                continue
            try:
                sh = float((row.get(share_col) or "0").replace(",", ""))
            except ValueError:
                sh = 0
            if sh <= 0:
                continue
            name = (row.get(name_col) or code).strip()
            positions.append({"code": code, "name": name, "shares": sh, "bucket": "default"})
    except Exception:
        pass
    return positions, cash_cny


def run_portfolio_trade(
    position_file: str,
    action: str,
    code: str | None = None,
    shares: float = 0.0,
    cash_delta: float = 0.0,
    cash_set: float | None = None,
    name: str | None = None,
    bucket: str | None = None,
) -> str:
    """
    交易变更命令：用于让 Agent 自动更新持仓文件。
    action: buy|sell|set-cash|cash-in|cash-out
    """
    position_file = os.path.abspath(position_file)
    if not os.path.exists(position_file):
        return f"持仓文件不存在：`{position_file}`，请先运行 `portfolio init`。"
    if position_file.lower().endswith(".csv"):
        return "暂不支持直接改写 CSV，请改用 markdown 持仓文件。"
    if yaml is None:
        return "缺少 pyyaml 依赖，无法自动改写 markdown 持仓文件。请先安装 `pyyaml`。"

    positions, cash_cny = parse_position_file(position_file, default_cash=0.0)
    action = action.lower().strip()

    if action == "set-cash":
        if cash_set is None:
            return "set-cash 需要传入 `--cash-set`。"
        cash_cny = max(0.0, float(cash_set))
    elif action in ("cash-in", "cash-out"):
        delta = abs(float(cash_delta))
        cash_cny += delta if action == "cash-in" else -delta
        cash_cny = max(0.0, cash_cny)
    elif action in ("buy", "sell"):
        if not code:
            return "buy/sell 需要传入 `--code`。"
        if shares <= 0:
            return "buy/sell 需要传入正数 `--shares`。"
        norm_code = str(code).strip()
        target = None
        for p in positions:
            if p["code"] == norm_code:
                target = p
                break
        if action == "buy":
            if target is None:
                positions.append({
                    "code": norm_code,
                    "name": name or norm_code,
                    "shares": float(shares),
                    "bucket": bucket or "default",
                })
            else:
                target["shares"] = float(target.get("shares", 0)) + float(shares)
                if name:
                    target["name"] = name
                if bucket:
                    target["bucket"] = bucket
        else:  # sell
            if target is None:
                return f"未找到持仓 {norm_code}，无法卖出。"
            target["shares"] = float(target.get("shares", 0)) - float(shares)
            if target["shares"] <= 0:
                positions = [p for p in positions if p["code"] != norm_code]
    else:
        return "不支持的 action，请使用 buy/sell/set-cash/cash-in/cash-out。"

    # 回写 markdown（保留可读结构）
    out = {
        "cash_cny": round(cash_cny, 2),
        "positions": [
            {
                "code": p["code"],
                "name": p.get("name", p["code"]),
                "shares": float(p.get("shares", 0)),
                "bucket": p.get("bucket", "default"),
            }
            for p in sorted(positions, key=lambda x: x["code"])
            if float(p.get("shares", 0)) > 0
        ],
    }
    content = (
        "# 当前持仓\n\n"
        "```yaml\n"
        + yaml.safe_dump({"cash_cny": out["cash_cny"]}, allow_unicode=True, sort_keys=False).strip()
        + "\n```\n\n```yaml\n"
        + yaml.safe_dump({"positions": out["positions"]}, allow_unicode=True, sort_keys=False).strip()
        + "\n```\n"
    )
    with open(position_file, "w", encoding="utf-8") as f:
        f.write(content)
    return f"持仓已更新：`{position_file}`（action={action}）"


def _analyze_position_technical(code: str, days: int = 120) -> dict:
    """
    对单只持仓做技术面摘要，用于叠加仓位建议。
    """
    try:
        payload = json.loads(run_analyze(code, days))
    except Exception as e:
        return {"code": code, "ok": False, "error": str(e), "bias": "neutral", "score": 0}
    if "error" in payload:
        return {"code": code, "ok": False, "error": payload["error"], "bias": "neutral", "score": 0}

    summary = payload.get("summary", {})
    signals = payload.get("signals", [])
    strong_bull = sum(1 for s in signals if s.get("strength") == "strong" and s.get("direction") == "bullish")
    strong_bear = sum(1 for s in signals if s.get("strength") == "strong" and s.get("direction") == "bearish")
    bull = int(summary.get("bullish_count", 0))
    bear = int(summary.get("bearish_count", 0))
    score = (bull - bear) + 2 * (strong_bull - strong_bear)
    top_signals = [s.get("name", "") for s in signals if s.get("strength") == "strong"][:3]
    return {
        "code": code,
        "ok": True,
        "bias": summary.get("bias", "neutral"),
        "bullish_count": bull,
        "bearish_count": bear,
        "strong_bullish_count": strong_bull,
        "strong_bearish_count": strong_bear,
        "score": score,
        "top_strong_signals": top_signals,
    }


def run_portfolio_daily_advice(
    position_file: str,
    cash_cny: float = 0.0,
    technical_days: int = 120,
    top_n: int = 5,
) -> str:
    """
    基于当日盯盘信号（含 TD Buy9）+ 仓位约束，输出用户可执行的每日建议。
    """
    positions, cash_from_file = parse_position_file(position_file, default_cash=cash_cny)
    if not positions:
        return "未读取到持仓，请先维护 `current_position.md`。"
    cash_cny = cash_cny if cash_cny > 0 else cash_from_file
    file_cfg = parse_strategy_config_from_position_file(position_file)

    max_single = _safe_num(file_cfg.get("max_single_weight"), STRATEGY_MAX_SINGLE_WEIGHT)
    min_cash = _safe_num(file_cfg.get("min_cash_weight"), STRATEGY_MIN_CASH_WEIGHT)
    top3_limit = _safe_num(file_cfg.get("top3_concentration_limit"), STRATEGY_TOP3_CONCENTRATION_LIMIT)

    total_value = cash_cny
    rows = []
    for p in positions:
        code = p["code"]
        market = detect_market(code)
        try:
            q = fetch_realtime_quote(code, market)
            price = _safe_num(q.get("price"), 0.0)
            if price <= 0:
                price = _safe_num(q.get("pre_close"), 0.0)
        except Exception:
            q = {}
            price = 0.0
        value = float(p["shares"]) * price if price > 0 else 0.0
        total_value += value
        rows.append({
            "code": code,
            "name": q.get("name", p.get("name", code)) if q else p.get("name", code),
            "shares": float(p.get("shares", 0)),
            "bucket": p.get("bucket", "default"),
            "price": price,
            "value": value,
        })
    if total_value <= 0:
        total_value = 1.0
    for r in rows:
        r["weight"] = r["value"] / total_value

    cash_weight = cash_cny / total_value
    rows_sorted = sorted(rows, key=lambda x: -x["weight"])
    top3_weight = sum(r["weight"] for r in rows_sorted[:3])

    tech_map = {}
    for r in rows_sorted[: max(1, int(top_n))]:
        try:
            payload = json.loads(run_analyze(r["code"], technical_days))
            tech_map[r["code"]] = payload
        except Exception:
            tech_map[r["code"]] = {"error": "analyze_failed", "signals": [], "summary": {"bias": "neutral"}}

    must_do = []
    can_do = []
    watch = []

    if cash_weight < min_cash:
        must_do.append(f"现金占比仅 {cash_weight:.1%}，低于建议下限 {min_cash:.0%}，今天优先控制加仓节奏。")
    if top3_weight > top3_limit:
        must_do.append(f"前三持仓合计 {top3_weight:.1%}，高于建议上限 {top3_limit:.0%}，优先做分散。")

    for r in rows_sorted:
        code = r["code"]
        name = r["name"]
        w = r["weight"]
        payload = tech_map.get(code, {})
        signals = payload.get("signals", []) if isinstance(payload, dict) else []
        has_buy9 = any("TD Buy 9" in str(s.get("name", "")) for s in signals)
        has_sell9 = any("TD Sell 9" in str(s.get("name", "")) for s in signals)
        strong_bear = any(
            s.get("strength") == "strong" and s.get("direction") == "bearish"
            for s in signals
        )

        if w > max_single:
            if has_sell9 or strong_bear:
                must_do.append(f"{name}（{code}）占比 {w:.1%} 且出现偏空信号，建议优先减仓。")
            else:
                must_do.append(f"{name}（{code}）占比 {w:.1%} 超过单票上限 {max_single:.0%}，建议分批降仓。")
            continue

        if has_buy9 and w < max_single and cash_weight >= min_cash:
            can_do.append(f"{name}（{code}）出现 TD Buy 9，可考虑小仓位分两笔试探加仓。")
        elif has_sell9:
            watch.append(f"{name}（{code}）出现 TD Sell 9，若后续走弱可先减一点锁定利润。")
        elif strong_bear:
            watch.append(f"{name}（{code}）出现强看空信号，建议提高警惕并观察支撑位。")

    if not must_do and not can_do:
        can_do.append("当前组合风险大体可控，今天以持有观察为主。")

    lines = [
        "# 每日操作建议（盯盘 + 持仓）",
        "",
        f"- 持仓文件：`{os.path.abspath(position_file)}`",
        f"- 资产总值（估算）：{total_value:,.0f} 元；现金占比：{cash_weight:.1%}",
        "",
        "## 今天优先执行（必须）",
    ]
    if must_do:
        for x in must_do:
            lines.append(f"- {x}")
    else:
        lines.append("- 暂无必须动作。")

    lines.append("")
    lines.append("## 可以考虑（机会）")
    for x in can_do:
        lines.append(f"- {x}")

    lines.append("")
    lines.append("## 继续观察")
    if watch:
        for x in watch:
            lines.append(f"- {x}")
    else:
        lines.append("- 暂无重点预警，按计划盯盘即可。")

    lines.append("\n⚠️ 建议基于公开行情与规则引擎，不构成投资建议。")
    return "\n".join(lines)


def run_portfolio_snapshot(
    position_file: str,
    cash_cny: float = 0.0,
    out_json_path: str | None = None,
    strategy_max_single: float | None = None,
    strategy_min_cash: float | None = None,
    strategy_top3_limit: float | None = None,
    rebalance_threshold: float | None = None,
    with_technical: bool = True,
    technical_days: int = 120,
    technical_top_n: int = 5,
) -> str:
    """
    拉取持仓行情，计算占比，应用策略，生成快照 JSON 与 Markdown 报告。
    返回 Markdown 报告字符串；若 out_json_path 给定则写入 JSON。
    """
    positions, cash_from_file = parse_position_file(position_file, default_cash=cash_cny)
    file_cfg = parse_strategy_config_from_position_file(position_file)
    strategy_max_single = _safe_num(
        strategy_max_single,
        _safe_num(file_cfg.get("max_single_weight"), STRATEGY_MAX_SINGLE_WEIGHT),
    )
    strategy_min_cash = _safe_num(
        strategy_min_cash,
        _safe_num(file_cfg.get("min_cash_weight"), STRATEGY_MIN_CASH_WEIGHT),
    )
    strategy_top3_limit = _safe_num(
        strategy_top3_limit,
        _safe_num(file_cfg.get("top3_concentration_limit"), STRATEGY_TOP3_CONCENTRATION_LIMIT),
    )
    rebalance_threshold = _safe_num(
        rebalance_threshold,
        _safe_num(file_cfg.get("rebalance_threshold"), STRATEGY_REBALANCE_THRESHOLD),
    )
    bucket_limits = file_cfg.get("asset_bucket_limits", {}) if isinstance(file_cfg, dict) else {}
    cash_cny = cash_cny if cash_cny > 0 else cash_from_file

    if not positions:
        return (
            "**未解析到有效持仓。**\n\n"
            "请提供 `current_position.md`（YAML 格式）或从东方财富/同花顺导出的 CSV。\n"
            "参见 `current_position.example.md` 模板。"
        )

    # 拉行情并计算市值
    total_value = cash_cny
    rows = []
    for p in positions:
        code = p["code"]
        market = detect_market(code)
        pre_price = _safe_num(p.get("price"), 0.0)
        pre_value = _safe_num(p.get("market_value"), 0.0)
        try:
            quote = fetch_realtime_quote(code, market)
            price = quote.get("price") or pre_price or 0
            if price <= 0 and quote.get("pre_close"):
                price = quote["pre_close"]
        except Exception:
            quote = {}
            price = pre_price if pre_price > 0 else 0
        name = quote.get("name", p["name"]) if quote else p["name"]
        shares = p["shares"]
        value = pre_value if pre_value > 0 else (shares * price if price > 0 else 0)
        total_value += value
        rows.append({
            "code": code,
            "name": name,
            "shares": shares,
            "price": price,
            "value": value,
            "bucket": p.get("bucket", "default"),
        })

    if total_value <= 0:
        total_value = 1.0  # 避免除零

    for r in rows:
        r["weight"] = r["value"] / total_value if total_value > 0 else 0
    cash_weight = cash_cny / total_value if total_value > 0 else 0

    # 按市值排序，计算前 3 集中度
    rows_sorted = sorted(rows, key=lambda x: -x["value"])
    top3_weight = sum(r["weight"] for r in rows_sorted[:3])
    bucket_weights = {}
    for r in rows:
        bk = str(r.get("bucket") or "default")
        bucket_weights[bk] = bucket_weights.get(bk, 0.0) + r["weight"]

    # 策略闸门
    gates = []
    if any(r["weight"] > strategy_max_single for r in rows):
        overweight = [r for r in rows if r["weight"] > strategy_max_single]
        gates.append({
            "gate": "single_overweight",
            "message": f"单只标的占比超过 {strategy_max_single:.0%}，建议减仓或分散",
            "items": [f"{r['code']} {r['name']} ({r['weight']:.1%})" for r in overweight],
        })
    if cash_weight < strategy_min_cash and cash_weight >= 0:
        gates.append({
            "gate": "cash_low",
            "message": f"现金占比 {cash_weight:.1%} 低于下限 {strategy_min_cash:.0%}，建议保留现金",
        })
    if top3_weight > strategy_top3_limit:
        gates.append({
            "gate": "concentration_high",
            "message": f"前 3 只标的合计占比 {top3_weight:.1%}，集中度过高，注意分散风险",
        })
    if isinstance(bucket_limits, dict):
        for bk, lim in bucket_limits.items():
            if not isinstance(lim, dict):
                continue
            w = bucket_weights.get(bk, 0.0)
            min_w = lim.get("min")
            max_w = lim.get("max")
            if min_w is not None and w < float(min_w):
                gates.append({
                    "gate": f"bucket_{bk}_underweight",
                    "message": f"资产桶 {bk} 占比 {w:.1%} 低于下限 {float(min_w):.0%}",
                })
            if max_w is not None and w > float(max_w):
                gates.append({
                    "gate": f"bucket_{bk}_overweight",
                    "message": f"资产桶 {bk} 占比 {w:.1%} 高于上限 {float(max_w):.0%}",
                })

    technical_map = {}
    if with_technical and rows_sorted:
        n = max(1, int(technical_top_n))
        for r in rows_sorted[:n]:
            technical_map[r["code"]] = _analyze_position_technical(r["code"], technical_days)

    # 推荐动作
    actions = []
    for r in rows_sorted:
        tech = technical_map.get(r["code"], {})
        if r["weight"] > strategy_max_single:
            if tech.get("score", 0) <= -2:
                actions.append(
                    f"**优先减仓** {r['code']} {r['name']}（占比 {r['weight']:.1%} 且技术面偏弱）"
                )
            else:
                actions.append(f"**减仓** {r['code']} {r['name']}（当前占比 {r['weight']:.1%}）")
        elif (
            with_technical
            and tech.get("ok")
            and tech.get("score", 0) >= 3
            and r["weight"] < max(0.0, strategy_max_single - rebalance_threshold)
            and cash_weight >= strategy_min_cash
        ):
            actions.append(
                f"**可考虑小幅加仓** {r['code']} {r['name']}（技术面偏强，当前占比 {r['weight']:.1%}）"
            )
    if cash_weight < strategy_min_cash and cash_weight >= 0:
        actions.append("**保留现金**，谨慎加仓")
    if not gates:
        actions.append("**持有**，当前占比在策略约束内")

    snapshot = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_status": get_market_status(),
        "position_source": "file",
        "cash_cny": cash_cny,
        "total_value_cny": total_value,
        "positions": [
            {
                "code": r["code"],
                "name": r["name"],
                "shares": r["shares"],
                "price": r["price"],
                "value_cny": round(r["value"], 2),
                "weight": round(r["weight"], 6),
                "bucket": r["bucket"],
            }
            for r in rows
        ],
        "strategy": {
            "max_single_weight": strategy_max_single,
            "min_cash_weight": strategy_min_cash,
            "top3_concentration_limit": strategy_top3_limit,
            "rebalance_threshold": rebalance_threshold,
            "asset_bucket_limits": bucket_limits,
        },
        "bucket_weights": {k: round(v, 6) for k, v in bucket_weights.items()},
        "technical_overlay": {
            "enabled": with_technical,
            "days": technical_days,
            "top_n": technical_top_n,
            "items": list(technical_map.values()),
        },
        "gates_triggered": gates,
        "recommended_actions": actions,
    }

    if out_json_path:
        with open(out_json_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

    report_md = _format_portfolio_report(snapshot)
    return report_md


def _format_portfolio_report(snapshot: dict) -> str:
    """将快照格式化为 Markdown 报告。"""
    lines = []
    ts = snapshot.get("timestamp", "")
    ms = snapshot.get("market_status", {})
    status_display = ms.get("display", "")
    src_display = "本地持仓文件"

    lines.append(f"# 持仓占比与策略建议\n")
    lines.append(f"⏰ 数据时间：{ts}  |  市场状态：{status_display}  |  持仓来源：{src_display}\n")
    lines.append("---\n")

    cash = snapshot.get("cash_cny", 0)
    total = snapshot.get("total_value_cny", 1)
    cash_w = cash / total if total > 0 else 0
    pos = snapshot.get("positions", [])
    top_pos = sorted(pos, key=lambda x: -x.get("weight", 0))

    lines.append("## 📝 持仓解读\n")
    if top_pos:
        top = top_pos[0]
        lines.append(
            f"- 你的第一大持仓是 `{top['name']}（{top['code']}）`，约占总资产 **{top['weight']:.1%}**。"
        )
    lines.append(f"- 当前现金约 **{cash:,.0f} 元**，占总资产 **{cash_w:.1%}**。")
    lines.append("- 下方表格会列出每个持仓的数量、市值和占比，便于你核对实际仓位。\n")

    lines.append("## 📊 持仓占比\n")
    lines.append("| 代码 | 名称 | 数量 | 最新价 | 市值(元) | 占比 |")
    lines.append("|------|------|------|--------|----------|------|")
    for p in snapshot.get("positions", []):
        lines.append(
            f"| {p['code']} | {p['name']} | {int(p['shares']):,} | "
            f"{p['price']:.3f} | {p['value_cny']:,.0f} | {p['weight']:.1%} |"
        )
    lines.append(f"| **现金** | 人民币 | - | - | **{cash:,.0f}** | **{cash_w:.1%}** |")
    lines.append(f"| **合计** | | | | **{total:,.0f}** | **100%** |\n")

    bucket_weights = snapshot.get("bucket_weights", {})
    if bucket_weights:
        lines.append("## 🧺 资产桶占比\n")
        for bk, w in sorted(bucket_weights.items(), key=lambda x: -x[1]):
            lines.append(f"- {bk}: {w:.1%}")
        lines.append("")

    lines.append("## 🎯 策略状态\n")
    gates = snapshot.get("gates_triggered", [])
    if not gates:
        lines.append("✅ 当前未触发风控闸门，持仓占比在策略约束内。\n")
    else:
        for g in gates:
            lines.append(f"- {g.get('message', '')}")
            for it in g.get("items", []):
                lines.append(f"  - {it}")
        lines.append("")

    tech = snapshot.get("technical_overlay", {})
    if tech.get("enabled"):
        lines.append("## 📈 技术面叠加\n")
        items = tech.get("items", [])
        if not items:
            lines.append("- 未生成技术面结果（可能数据获取失败）\n")
        else:
            for it in items:
                if not it.get("ok"):
                    lines.append(f"- `{it.get('code', '')}`：技术面暂时获取失败")
                    continue
                sig = "、".join(it.get("top_strong_signals", [])[:2]) or "无显著强信号"
                bias_map = {"bullish": "偏强", "bearish": "偏弱", "neutral": "中性"}
                bias_text = bias_map.get(it.get("bias", "neutral"), "中性")
                score = it.get("score", 0)
                score_text = "偏多" if score >= 3 else ("偏空" if score <= -3 else "均衡")
                lines.append(
                    f"- `{it['code']}`：技术面{bias_text}，综合判断{score_text}；重点信号：{sig}"
                )
        lines.append("")

    lines.append("## 💼 操作建议\n")
    actions = snapshot.get("recommended_actions", [])
    if not actions:
        lines.append("- 暂无明确调仓动作，建议继续观察。")
    for a in actions:
        if "减仓" in a:
            lines.append(f"- {a}。主要原因是仓位或风险约束已触发。")
        elif "加仓" in a:
            lines.append(f"- {a}。这是在仓位允许前提下，基于技术面偏强给出的提示。")
        elif "现金" in a:
            lines.append(f"- {a}。优先保证组合的流动性与防守空间。")
        else:
            lines.append(f"- {a}")
    lines.append("")
    lines.append("---\n⚠️ 以上基于持仓占比与策略规则，不构成投资建议。\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 持续盯盘
# ---------------------------------------------------------------------------

def _emit(event: str, data: dict):
    """输出一行 JSONL 事件到 stdout，供 Agent 逐行读取。"""
    line = {"event": event, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    line.update(data)
    print(json.dumps(line, ensure_ascii=False), flush=True)


def run_monitor(code: str, mode: str, interval: int, days: int):
    """持续盯盘主循环。

    mode=periodic : 交易时段内每 interval 秒输出一次完整报告
    mode=signal   : 每 interval 秒检查一次，仅在出现新强信号或价格剧烈波动时输出
    """
    seen_strong = set()
    anchor_price = None
    tick = 0

    _emit("monitor_start", {
        "code": code, "mode": mode, "interval_sec": interval,
        "message": f"开始盯盘 {code}，模式={mode}，间隔={interval}s",
    })

    try:
        while True:
            ms = get_market_status()

            # ── 非交易时段：发一条等待事件，然后休眠 ──────────
            if ms["status"] not in ("trading", "auction"):
                _emit("waiting", {
                    "code": code,
                    "market_status": ms["display"],
                    "message": f"{ms['display']}，{interval}s 后再检查",
                })
                time.sleep(interval)
                continue

            # ── 跑一次完整分析 ─────────────────────────────────
            try:
                result = json.loads(run_analyze(code, days))
            except Exception as e:
                _emit("error", {"code": code, "detail": str(e)})
                time.sleep(interval)
                continue

            if "error" in result:
                _emit("error", {"code": code, "detail": result["error"]})
                time.sleep(interval)
                continue

            tick += 1
            cur_price = result.get("quote", {}).get("price", 0)

            # ── periodic 模式：每次都推完整报告 ───────────────
            if mode == "periodic":
                _emit("report", result)

            # ── signal 模式：只在有事发生时推 ─────────────────
            elif mode == "signal":
                cur_strong = {
                    s["name"] for s in result.get("signals", [])
                    if s["strength"] == "strong"
                }
                new_signals = cur_strong - seen_strong

                price_jump = False
                if anchor_price and cur_price:
                    delta_pct = (cur_price - anchor_price) / anchor_price * 100
                    if abs(delta_pct) >= 2:
                        price_jump = True

                if new_signals or price_jump:
                    result["new_strong_signals"] = sorted(new_signals)
                    result["price_jump"] = price_jump
                    if anchor_price:
                        result["price_vs_anchor"] = round(
                            (cur_price - anchor_price) / anchor_price * 100, 2)
                    _emit("alert", result)
                    anchor_price = cur_price
                else:
                    # 轻量心跳，每 5 轮发一次，让 Agent 知道还活着
                    if tick % 5 == 0:
                        _emit("heartbeat", {
                            "code": code, "tick": tick,
                            "price": cur_price,
                            "strong_count": len(cur_strong),
                            "message": "无新强信号",
                        })

                seen_strong = cur_strong
                if anchor_price is None:
                    anchor_price = cur_price

            time.sleep(interval)

    except KeyboardInterrupt:
        _emit("monitor_stop", {"code": code, "ticks": tick, "message": "盯盘已停止"})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="A股智能盯盘助手")
    sub = parser.add_subparsers(dest="command")

    p_search = sub.add_parser("search", help="搜索股票")
    grp = p_search.add_mutually_exclusive_group(required=True)
    grp.add_argument("--keyword-file", help="从UTF-8文件读取关键字")
    grp.add_argument("--keyword-hex", help="UTF-8十六进制编码的关键字")
    grp.add_argument("--code", help="按股票代码搜索")

    p_analyze = sub.add_parser("analyze", help="分析股票")
    p_analyze.add_argument("code", help="股票代码，如 600519")
    p_analyze.add_argument("--days", type=int, default=120, help="回溯交易日数")

    p_monitor = sub.add_parser("monitor", help="持续盯盘")
    p_monitor.add_argument("code", help="股票代码，如 600519")
    p_monitor.add_argument("--mode", default="periodic", choices=["periodic", "signal"],
                           help="periodic=定时播报 | signal=仅强信号提醒")
    p_monitor.add_argument("--interval", type=int, default=600,
                           help="检查间隔（秒），默认600（10分钟）")
    p_monitor.add_argument("--days", type=int, default=120, help="回溯交易日数")

    p_portfolio = sub.add_parser("portfolio", help="持仓快照与策略建议")
    p_port_sub = p_portfolio.add_subparsers(dest="portfolio_command")
    p_init = p_port_sub.add_parser("init", help="初始化持仓模板文件")
    p_init.add_argument("--position-file", default="current_position.md", help="输出模板路径")
    p_init.add_argument("--force", action="store_true", help="强制覆盖已有文件")

    p_trade = p_port_sub.add_parser("trade", help="按交易指令更新持仓文件")
    p_trade.add_argument("--position-file", default="current_position.md", help="持仓文件路径")
    p_trade.add_argument("--action", required=True, choices=["buy", "sell", "set-cash", "cash-in", "cash-out"])
    p_trade.add_argument("--code", help="股票代码，buy/sell 必填")
    p_trade.add_argument("--shares", type=float, default=0.0, help="交易股数，buy/sell 必填")
    p_trade.add_argument("--cash-delta", type=float, default=0.0, help="现金变动额，cash-in/out 使用")
    p_trade.add_argument("--cash-set", type=float, help="直接设定现金，set-cash 使用")
    p_trade.add_argument("--name", help="买入新标的时可选名称")
    p_trade.add_argument("--bucket", help="买入新标的时可选资产桶")

    p_strategy = p_port_sub.add_parser("strategy", help="查看默认/覆盖/生效策略参数")
    p_strategy.add_argument("--position-file", default="current_position.md", help="持仓文件路径")

    p_advice = p_port_sub.add_parser("advice", help="生成每日操作建议（结合盯盘信号）")
    p_advice.add_argument("--position-file", default="current_position.md", help="持仓文件路径")
    p_advice.add_argument("--cash", type=float, default=0.0, help="现金（元），>0 时覆盖文件内现金")
    p_advice.add_argument("--technical-days", type=int, default=120, help="技术面回溯天数")
    p_advice.add_argument("--top-n", type=int, default=5, help="分析前N大持仓")

    p_snap = p_port_sub.add_parser("snapshot", help="生成持仓占比与策略建议")
    p_snap.add_argument("--position-file", default="current_position.md",
                        help="本地持仓文件（md/csv）路径")
    p_snap.add_argument("--cash", type=float, default=0.0,
                        help="现金（元），>0 时覆盖文件内现金")
    p_snap.add_argument("--out", help="快照 JSON 输出路径")
    p_snap.add_argument("--max-single-weight", type=float, default=None)
    p_snap.add_argument("--min-cash-weight", type=float, default=None)
    p_snap.add_argument("--top3-limit", type=float, default=None)
    p_snap.add_argument("--rebalance-threshold", type=float, default=None)
    p_snap.add_argument("--technical-days", type=int, default=120)
    p_snap.add_argument("--technical-top-n", type=int, default=5)
    p_snap.add_argument("--no-technical", action="store_true", help="关闭技术面叠加")

    args = parser.parse_args()

    try:
        if args.command == "search":
            if args.keyword_file:
                with open(args.keyword_file, encoding="utf-8") as f:
                    keyword = f.read().strip()
            elif args.keyword_hex:
                keyword = bytes.fromhex(args.keyword_hex).decode("utf-8")
            else:
                keyword = args.code
            print(run_search(keyword))
        elif args.command == "analyze":
            print(run_analyze(args.code, args.days))
        elif args.command == "monitor":
            run_monitor(args.code, args.mode, args.interval, args.days)
        elif args.command == "portfolio" and args.portfolio_command == "init":
            print(run_portfolio_init(
                position_file=args.position_file,
                force=args.force,
            ))
        elif args.command == "portfolio" and args.portfolio_command == "trade":
            print(run_portfolio_trade(
                position_file=args.position_file,
                action=args.action,
                code=args.code,
                shares=args.shares,
                cash_delta=args.cash_delta,
                cash_set=args.cash_set,
                name=args.name,
                bucket=args.bucket,
            ))
        elif args.command == "portfolio" and args.portfolio_command == "strategy":
            print(run_portfolio_strategy(position_file=args.position_file))
        elif args.command == "portfolio" and args.portfolio_command == "advice":
            print(run_portfolio_daily_advice(
                position_file=args.position_file,
                cash_cny=args.cash,
                technical_days=args.technical_days,
                top_n=args.top_n,
            ))
        elif args.command == "portfolio" and args.portfolio_command == "snapshot":
            print(run_portfolio_snapshot(
                position_file=args.position_file,
                cash_cny=args.cash,
                out_json_path=args.out,
                strategy_max_single=args.max_single_weight,
                strategy_min_cash=args.min_cash_weight,
                strategy_top3_limit=args.top3_limit,
                rebalance_threshold=args.rebalance_threshold,
                with_technical=not args.no_technical,
                technical_days=args.technical_days,
                technical_top_n=args.technical_top_n,
            ))
        else:
            parser.print_help()
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": f"{e.__class__.__name__}: {e}"}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
