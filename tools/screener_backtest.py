#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动量+价值选股筛 · 历史信号重放回测。

把 stock_screener.py 的规则原样跑在过去 N 年的日线上（直接 import 其
check_momentum / check_value / grade_signal，不重写规则），回答：
1. 动量层（60日新高+放量）信号的历史胜率 / 收益分布 / 相对 SPY 超额；
2. 观察池（DEFAULT_WATCHLIST）与对照组（含表现平庸的知名股）差异——幸存者偏差体检；
3. 有基本面数据的标的（NVDA/AMD/MU）按 6 维分级后的表现差异。

方法约束（防未来函数）：
- 信号日只用截至当日的价格窗口（check_momentum 本身只看窗口内数据）；
- 6 维验证用 check_value(signal_date=...)，只取信号日之前发布的财报；
- 同一标的信号触发后 20 个交易日冷却，避免突破日聚簇重复计数；
- 前向收益为信号日收盘 → N 个交易日后收盘，不足 N 日的信号丢弃。

用法：
  python3 screener_backtest.py                        # 默认股票池 + 5 年
  python3 screener_backtest.py --tickers NVDA,INTC --years 3
  python3 screener_backtest.py --output ../reports/选股筛回测-20260709.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stock_screener  # noqa: E402  复用规则真源：check_momentum / check_value / grade_signal

COOLDOWN_TRADING_DAYS = 20
HORIZONS = (5, 20, 60)  # 前向交易日
BENCHMARK = "SPY"
FETCH_DELAY_SECONDS = 0.6

# 观察池：ai-berkshire DEFAULT_WATCHLIST 的美股/港股部分（CRWV/CRCL 上市晚，数据自动截短）
WATCHLIST_POOL = [
    "NVDA", "AMD", "MU", "AVGO", "MRVL", "TSM",
    "GOOG", "META", "MSFT", "AMZN", "CRM", "NOW", "PLTR",
    "ETN", "PWR", "VRT",
    "COIN", "HOOD", "MSTR",
    "0700.HK", "9888.HK", "1024.HK",
]
# 对照组：2021 年前上市、其后表现平庸或落后的知名股票（对抗幸存者偏差）
CONTROL_POOL = [
    "INTC", "PYPL", "DIS", "NKE", "BABA", "T", "VZ", "F",
    "WBA", "MMM", "ZM", "PTON", "ROKU", "SNAP", "SHOP",
    "BA", "UPS", "TGT", "PFE", "MRNA", "KO", "CVX",
]


def fetch_history(ticker: str, years: int) -> list:
    """拉取 N 年日线，行格式与 stock_screener.fetch_prices_curl 一致。"""
    end_ts = int(time.time())
    start_ts = end_ts - years * 365 * 86400
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?period1={start_ts}&period2={end_ts}&interval=1d"
    )
    try:
        result = subprocess.run(
            ["curl", "-s", "-H", "User-Agent: Mozilla/5.0", url],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        chart = data.get("chart", {}).get("result", [{}])[0]
        timestamps = chart.get("timestamp", [])
        quote = chart.get("indicators", {}).get("quote", [{}])[0]
        rows = []
        for i, ts in enumerate(timestamps):
            c = quote.get("close", [None] * len(timestamps))[i]
            v = quote.get("volume", [None] * len(timestamps))[i]
            h = quote.get("high", [None] * len(timestamps))[i]
            if c and v and h:
                rows.append({
                    "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
                    "close": c, "high": h, "volume": v,
                })
        return rows
    except Exception as exc:
        print(f"  {ticker}: 拉取失败 {exc}", file=sys.stderr)
        return []


def replay_ticker(ticker: str, prices: list) -> list:
    """滑窗重放：返回该标的全部动量信号（含前向收益与时点分级）。"""
    signals = []
    cooldown_until = -1
    max_horizon = max(HORIZONS)
    for i in range(61, len(prices) - max_horizon):
        if i <= cooldown_until:
            continue
        momentum = stock_screener.check_momentum(prices[: i + 1])
        if not momentum or not momentum["triggered"]:
            continue
        cooldown_until = i + COOLDOWN_TRADING_DAYS

        signal_close = prices[i]["close"]
        record = {
            "ticker": ticker,
            "date": prices[i]["date"],
            "index": i,
            "close": signal_close,
            "vol_ratio": momentum["vol_ratio"],
            "pct_30d": momentum["pct_30d"],
        }
        for horizon in HORIZONS:
            fwd = prices[i + horizon]["close"]
            record[f"ret_{horizon}d"] = (fwd / signal_close - 1) * 100

        # 时点分级（仅 fundamentals.json 覆盖的标的有 6 维结果）
        value = stock_screener.check_value(ticker, signal_date=record["date"])
        grade, reason, _ = stock_screener.grade_signal(momentum, value)
        record["grade"] = grade
        record["score"] = value["score"] if value else None
        signals.append(record)
    return signals


def index_benchmark(prices: list) -> dict:
    """date -> index 映射，用于对齐基准前向收益。"""
    return {row["date"]: i for i, row in enumerate(prices)}


def attach_benchmark(signals: list, bench_prices: list) -> None:
    """给每个信号附上同窗口 SPY 收益与超额收益。"""
    bench_idx = index_benchmark(bench_prices)
    max_horizon = max(HORIZONS)
    for record in signals:
        i = bench_idx.get(record["date"])
        if i is None or i + max_horizon >= len(bench_prices):
            # 港股交易日与美股基准错位时，取信号日前最近的基准交易日
            earlier = [d for d in bench_idx if d <= record["date"]]
            if not earlier:
                continue
            i = bench_idx[max(earlier)]
            if i + max_horizon >= len(bench_prices):
                continue
        base = bench_prices[i]["close"]
        for horizon in HORIZONS:
            bench_ret = (bench_prices[i + horizon]["close"] / base - 1) * 100
            record[f"excess_{horizon}d"] = record[f"ret_{horizon}d"] - bench_ret


def summarize(signals: list, key_prefix: str = "ret") -> dict:
    """按 horizon 汇总：样本数 / 胜率 / 均值 / 中位数。"""
    out = {}
    for horizon in HORIZONS:
        key = f"{key_prefix}_{horizon}d"
        rets = [s[key] for s in signals if key in s]
        if not rets:
            continue
        out[horizon] = {
            "n": len(rets),
            "win": sum(1 for r in rets if r > 0) / len(rets) * 100,
            "mean": statistics.mean(rets),
            "median": statistics.median(rets),
        }
    return out


def fmt_summary_row(label: str, stats: dict) -> str:
    cells = [label]
    for horizon in HORIZONS:
        s = stats.get(horizon)
        cells.append(
            f"{s['win']:.0f}% / {s['mean']:+.1f}% / {s['median']:+.1f}%" if s else "-"
        )
    n = next((s["n"] for s in stats.values()), 0)
    cells.append(str(n))
    return "| " + " | ".join(cells) + " |"


def build_report(all_signals: list, pool_of: dict, years: int, failed: list) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    watch = [s for s in all_signals if pool_of.get(s["ticker"]) == "watch"]
    control = [s for s in all_signals if pool_of.get(s["ticker"]) == "control"]

    lines = [
        f"# 选股筛动量框架 · 历史信号重放回测（{today}）",
        "",
        "## 方法",
        "",
        f"- 规则：直接调用 `tools/stock_screener.py` 的 check_momentum（60日新高或近5日突破 + 5/20日量比>1.5），未重写；",
        f"- 区间：近 {years} 年日线（Yahoo Finance）；同标的信号后 {COOLDOWN_TRADING_DAYS} 个交易日冷却去重；",
        f"- 前向收益：信号日收盘 → {'/'.join(str(h) for h in HORIZONS)} 个交易日后收盘；超额 = 信号收益 − 同窗口 {BENCHMARK} 收益；",
        "- 6 维分级用 check_value(signal_date) 时点过滤，只取信号日前已发布的财报（仅覆盖已录入基本面的标的）。",
        "",
        "## 一、动量层总体表现",
        "",
        "格式：胜率 / 平均 / 中位数",
        "",
        "| 分组 | " + " | ".join(f"{h}日" for h in HORIZONS) + " | 信号数 |",
        "|---|" + "---|" * (len(HORIZONS) + 1),
        fmt_summary_row("全部信号（绝对收益）", summarize(all_signals)),
        fmt_summary_row("全部信号（超额 vs SPY）", summarize(all_signals, "excess")),
        fmt_summary_row("观察池（绝对）", summarize(watch)),
        fmt_summary_row("对照组（绝对）", summarize(control)),
        fmt_summary_row("观察池（超额）", summarize(watch, "excess")),
        fmt_summary_row("对照组（超额）", summarize(control, "excess")),
        "",
    ]

    # 按年份
    by_year = defaultdict(list)
    for s in all_signals:
        by_year[s["date"][:4]].append(s)
    lines += [
        "## 二、按年份（绝对收益）",
        "",
        "| 年份 | " + " | ".join(f"{h}日" for h in HORIZONS) + " | 信号数 |",
        "|---|" + "---|" * (len(HORIZONS) + 1),
    ]
    for year in sorted(by_year):
        lines.append(fmt_summary_row(year, summarize(by_year[year])))
    lines.append("")

    # 分级窄测
    graded = [s for s in all_signals if s.get("score") is not None]
    lines += [
        "## 三、6 维分级窄测（仅基本面已录入的标的）",
        "",
    ]
    if graded:
        by_grade = defaultdict(list)
        for s in graded:
            by_grade[s["grade"]].append(s)
        lines += [
            "| 分级 | " + " | ".join(f"{h}日" for h in HORIZONS) + " | 信号数 |",
            "|---|" + "---|" * (len(HORIZONS) + 1),
        ]
        for grade in sorted(by_grade):
            lines.append(fmt_summary_row(grade, summarize(by_grade[grade])))
        covered = sorted({s["ticker"] for s in graded})
        lines += ["", f"覆盖标的：{', '.join(covered)}（样本极少，仅供方向参考）"]
    else:
        lines.append("（区间内无带基本面数据的信号）")
    lines.append("")

    # 最好/最差信号
    ranked = sorted(
        (s for s in all_signals if "ret_60d" in s), key=lambda s: s["ret_60d"]
    )
    lines += ["## 四、极值信号（60日收益）", ""]
    for s in ranked[-5:][::-1]:
        lines.append(f"- 🟢 {s['ticker']} {s['date']}：+{s['ret_60d']:.1f}%")
    for s in ranked[:5]:
        lines.append(f"- 🔴 {s['ticker']} {s['date']}：{s['ret_60d']:.1f}%")
    lines.append("")

    if failed:
        lines += [f"> 数据拉取失败/不足的标的（未计入）：{', '.join(failed)}", ""]
    lines += [
        "## 局限（阅读结论前必看）",
        "",
        "- 股票池为人工选定，观察池天然偏向已知强势股；对照组用于校准该偏差，"
        "两组差异比绝对数字更有信息量；",
        "- 收盘价成交假设，未计滑点、手续费；信号可能盘中触发而回测按收盘计；",
        "- 60 日冷却外的重叠持仓未做组合级模拟，收益不能直接线性外推为组合收益；",
        "- 6 维分级样本极少（3 只标的的手工财报数据），其结论强度远低于动量层；",
        "- 历史表现不代表未来。本报告由 AI 生成，仅供研究参考，不构成投资建议。",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="选股筛动量框架历史回测")
    parser.add_argument("--tickers", help="逗号分隔标的（默认观察池+对照组）")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--output", help="报告输出路径（默认 stdout）")
    parser.add_argument("--signals-json", help="同时导出信号明细 JSON")
    args = parser.parse_args()

    if args.tickers:
        pool_of = {t.strip().upper(): "watch" for t in args.tickers.split(",") if t.strip()}
    else:
        pool_of = {t: "watch" for t in WATCHLIST_POOL}
        pool_of.update({t: "control" for t in CONTROL_POOL})

    bench_prices = fetch_history(BENCHMARK, args.years)
    if len(bench_prices) < 200:
        print(f"基准 {BENCHMARK} 数据拉取失败，无法计算超额收益", file=sys.stderr)
        return 1

    all_signals: list = []
    failed: list = []
    for ticker in pool_of:
        time.sleep(FETCH_DELAY_SECONDS)
        prices = fetch_history(ticker, args.years)
        if len(prices) < 61 + max(HORIZONS):
            failed.append(ticker)
            continue
        signals = replay_ticker(ticker, prices)
        print(f"  {ticker:<8} {len(prices)} 天 -> {len(signals)} 个信号", file=sys.stderr)
        all_signals.extend(signals)

    if not all_signals:
        print("无任何信号，检查数据源", file=sys.stderr)
        return 1
    attach_benchmark(all_signals, bench_prices)

    report = build_report(all_signals, pool_of, args.years, failed)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"报告已写入 {out}", file=sys.stderr)
    else:
        print(report)
    if args.signals_json:
        Path(args.signals_json).write_text(
            json.dumps(all_signals, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
