#!/usr/bin/env python3
"""生成 AI Berkshire 研究工作表的脚本，供 GitHub Actions 调用。"""
import os
import sys
from datetime import date

stocks = os.environ.get("STOCKS", "NVDA,AAPL,GOOGL,MSFT,AMZN")
today = date.today().isoformat()
report_dir = f"reports/{today}"
os.makedirs(report_dir, exist_ok=True)

for stock in stocks.split(","):
    stock = stock.strip()
    if not stock:
        continue
    lines = [
        f"# {stock} 研究工作表",
        f"## 日期: {today}",
        "## 框架: AI Berkshire",
        "",
        "## 1. 财务数据清单",
        "- 营收 & 利润趋势 (近5年)",
        "- ROE / ROIC",
        "- 自由现金流",
        "- 负债结构",
        "",
        "## 2. 估值检查",
        "- 当前 PE / PB / PS",
        "- 历史估值百分位",
        "- DCF 估值区间",
        "",
        "## 3. 护城河分析",
        "- 品牌 / 网络效应 / 转换成本",
        "- 专利 & 技术壁垒",
        "- 市场份额变化",
        "",
        "## 4. 管理层评估",
        "- 创始人/CEO 背景",
        "- 资本配置历史",
        "- 股权结构",
        "",
        "## 5. 风险评估",
        "- 行业竞争",
        "- 监管风险",
        "- 技术颠覆",
        "",
        "## 初步判断",
        "> 待 AI 分析师完成研究后填写",
        "",
        "---",
        "*由 AI Berkshire 自动生成*",
    ]
    path = f"{report_dir}/{stock}_worksheet.md"
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  -> {path}")

print(f"\n完成! 共生成 {len(os.listdir(report_dir))} 个工作表")
