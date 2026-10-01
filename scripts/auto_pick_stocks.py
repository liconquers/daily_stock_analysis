# -*- coding: utf-8 -*-
"""
动态量化选股、大盘风控与战绩追踪闭环引擎 (DSA Auto-Pick & Risk Guard & Tracker)
版本: v3.0 (全量化闭环版: P0熔断 + P1共振 + P2决策看板 + P3战绩追踪)
"""

import argparse
import base64
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("auto_pick_stocks_v3")

GITHUB_REPO = "liconquers/daily_stock_analysis"
HISTORY_FILE_PATH = "data/picks_history.json"


def send_telegram(text: str) -> bool:
    """向 Telegram 发送格式化 Markdown 消息"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        logger.warning("未配置 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，跳过 TG 发送")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true"
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=payload, headers={"User-Agent": "DailyStockAnalysis/3.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        logger.info(f"Telegram 消息推送成功，HTTP 状态码: {resp.status}")
        return True
    except Exception as e:
        logger.error(f"Telegram 推送失败: {e}")
        return False


def get_history_from_github(token: str) -> tuple[dict, str]:
    """从 GitHub API 读取持久化的战绩历史"""
    if not token:
        return {}, ""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{HISTORY_FILE_PATH}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "User-Agent": "DailyStockAnalysisTracker",
        "Accept": "application/vnd.github.v3+json"
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
        content = base64.b64decode(data["content"]).decode("utf-8")
        return json.loads(content), data["sha"]
    except Exception as e:
        logger.info(f"历史战绩文件不存在或首次创建: {e}")
        return {}, ""


def save_history_to_github(token: str, history_data: dict, sha: str = "") -> None:
    """通过 GitHub API 保存最新的战绩记录"""
    if not token:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{HISTORY_FILE_PATH}"
    content_bytes = json.dumps(history_data, ensure_ascii=False, indent=2).encode("utf-8")
    encoded_content = base64.b64encode(content_bytes).decode("utf-8")

    payload = {
        "message": "chore: update picks history and T+1 tracking records",
        "content": encoded_content,
        "branch": "main"
    }
    if sha:
        payload["sha"] = sha

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
        "User-Agent": "DailyStockAnalysisTracker"
    }, method="PUT")

    try:
        resp = urllib.request.urlopen(req, timeout=10)
        logger.info(f"历史战绩记录已成功持久化至 GitHub! 状态: {resp.status}")
    except Exception as e:
        logger.error(f"保存历史战绩到 GitHub 失败: {e}")


def calculate_t_plus_one_summary(last_record: dict, current_snapshot_df) -> str:
    """[P3 战绩追踪] 对比上一期推荐股票的今日 T+1 表现"""
    if not last_record or "picks" not in last_record:
        return ""

    last_picks = last_record["picks"]
    if not last_picks:
        return ""

    last_date = last_record.get("date", "上期")
    last_slot = "早盘" if last_record.get("slot") == "morning" else "尾盘"

    # 将快照数据转换为字典加快查询: code -> row
    quotes_map = {}
    if current_snapshot_df is not None and not current_snapshot_df.empty:
        for _, row in current_snapshot_df.iterrows():
            c = str(row.get("code", "")).strip()
            if c:
                quotes_map[c] = row

    wins = 0
    total = len(last_picks)
    tracked_lines = []

    for item in last_picks:
        code = item["code"]
        name = item["name"]
        entry_price = item.get("price", 0.0)

        cur = quotes_map.get(code)
        if cur is not None and entry_price > 0:
            cur_price = float(cur.get("price", 0.0)) or entry_price
            diff_pct = ((cur_price - entry_price) / entry_price) * 100.0
            if diff_pct > 0:
                wins += 1
                icon = "🟢"
            else:
                icon = "🔴"
            tracked_lines.append(f"{icon} `{code}` {name}: `{diff_pct:+.2f}%` (入场 {entry_price:.2f} $\\rightarrow$ 现 {cur_price:.2f})")
        else:
            tracked_lines.append(f"⚪ `{code}` {name}: 行情同步中")

    win_rate = (wins / total * 100.0) if total > 0 else 0.0

    summary_text = (
        f"📊 *【P3 战绩闭环 · {last_date} {last_slot} T+1 追踪】*\n"
        f"• **胜率表现**: `{wins}/{total}` (胜率: `{win_rate:.1f}%`)\n"
        + "\n".join(tracked_lines[:5]) + ("\n• ...(其余持平或跟踪中)" if len(tracked_lines) > 5 else "")
        + "\n━━━━━━━━━━━━━━━━━━━\n\n"
    )
    return summary_text


def assess_market_regime(df_snapshot) -> dict:
    """[P0 核心] 大盘环境与风险状态评估 (空仓熔断)"""
    total = len(df_snapshot)
    if total < 500:
        return {"status": "normal", "target_count": 10, "reason": "快照样本不足"}

    change_pcts = df_snapshot["change_pct"].dropna()
    up_count = int((change_pcts > 0).sum())
    down_count = int((change_pcts < 0).sum())
    median_pct = float(change_pcts.median())

    logger.info(f"📊 全市场: 总数={total}, 上涨={up_count}, 下跌={down_count}, 中位数={median_pct:+.2f}%")

    # 触发空仓熔断: 暴跌超 3800 家且上涨少于 1000 家，或中位数跌破 -2.0%
    if (down_count >= 3800 and up_count <= 1000) or median_pct <= -2.0:
        alert_msg = (
            "🚨 *【大盘风控熔断警报 · 坚决空仓防守】*\n\n"
            f"• 今日全市场上涨: `{up_count}` 家 | 下跌: `{down_count}` 家\n"
            f"• 市场涨跌中位数: `{median_pct:+.2f}%`\n"
            "• **风控判定**: 市场遭遇系统性破位杀跌，泥沙俱下！\n\n"
            "🛡️ **交易纪律**: **【已触发系统空仓熔断】今日停止推送个股**，严禁追高与抄底，保住本金！"
        )
        return {"status": "circuit_breaker", "target_count": 0, "alert_msg": alert_msg}

    # 震荡弱势防守市
    if up_count < 2000:
        return {
            "status": "cautious",
            "target_count": 5,
            "up_count": up_count,
            "down_count": down_count,
            "median_pct": median_pct,
            "title": "震荡分化市 · 精选防守 (5只)"
        }

    # 强势多头市
    return {
        "status": "bullish",
        "target_count": 10,
        "up_count": up_count,
        "down_count": down_count,
        "median_pct": median_pct,
        "title": "多头强势市 · 积极进攻 (10只)"
    }


def pick_stocks(time_slot: str = "morning", top_k: int = 10) -> str:
    """量化智能选股完整闭环流程"""
    from src.services.screening.snapshot import fetch_snapshot_with_fallback
    from src.services.screening.pipeline import screen

    logger.info(f"=== 开始执行量化筛选流程 (时段: {time_slot}) ===")
    gh_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN", "")

    # 1. 获取全市场快照与大盘风控 (P0)
    snapshot_df = None
    try:
        snapshot_df = fetch_snapshot_with_fallback(
            sources=["sina", "efinance", "akshare_em", "em_datacenter"],
            market="cn"
        )
        regime = assess_market_regime(snapshot_df)
        if regime["status"] == "circuit_breaker":
            send_telegram(regime["alert_msg"])
            return ""
        effective_k = min(top_k, regime.get("target_count", top_k))
    except Exception as e:
        logger.warning(f"大盘风控评估轻微异常，回退默认模式: {e}")
        effective_k = top_k
        regime = {"status": "normal", "title": "量化选股模型", "up_count": 0, "down_count": 0, "median_pct": 0.0}

    # 2. [P3 战绩追踪] 核算上期 T+1 表现
    history_db, history_sha = get_history_from_github(gh_token)
    t_plus_one_card = ""
    if history_db and "last_run" in history_db:
        t_plus_one_card = calculate_t_plus_one_summary(history_db["last_run"], snapshot_df)

    # 3. 运行策略初筛 (P1)
    strategy_name = "volume_breakout" if time_slot == "morning" else "shrink_pullback"
    slot_title = "早盘放量突破" if time_slot == "morning" else "尾盘缩量低吸"

    try:
        res = screen(strategy=strategy_name, market="cn", max_output=effective_k * 3)
        raw_picks = res.picks or []

        valid_picks = []
        for p in raw_picks:
            code = str(p.code).strip()
            pct = getattr(p, "change_pct", 0.0) or 0.0
            price = getattr(p, "price", 0.0) or 0.0
            name = getattr(p, "name", code) or code
            amount = getattr(p, "amount", 0.0) or 0.0
            volume_ratio = getattr(p, "volume_ratio", 1.0) or 1.0
            turnover = getattr(p, "turnover_rate", 1.0) or 1.0

            # 过滤涨停板与流动性陷阱
            if pct >= 9.8 or (code.startswith(("30", "68")) and pct >= 19.8):
                continue
            if amount > 0 and amount < 80000000:
                continue

            valid_picks.append({
                "code": code,
                "name": name,
                "price": price,
                "pct": pct,
                "volume_ratio": volume_ratio,
                "turnover": turnover,
                "buy_min": round(price * 0.985, 2),
                "buy_max": round(price * 1.005, 2),
                "stop_loss": round(price * 0.95, 2)
            })
            if len(valid_picks) >= effective_k:
                break

        # 候选不足时补充
        if len(valid_picks) < effective_k:
            fallback_res = screen(strategy="capital_heat", market="cn", max_output=effective_k)
            for p in (fallback_res.picks or []):
                code = str(p.code).strip()
                if not any(x["code"] == code for x in valid_picks):
                    pct = getattr(p, "change_pct", 0.0) or 0.0
                    price = getattr(p, "price", 0.0) or 0.0
                    name = getattr(p, "name", code) or code
                    if pct < 9.8 and not (code.startswith(("30", "68")) and pct >= 19.8):
                        valid_picks.append({
                            "code": code, "name": name, "price": price, "pct": pct,
                            "volume_ratio": 2.0, "turnover": 2.0,
                            "buy_min": round(price * 0.985, 2),
                            "buy_max": round(price * 1.005, 2),
                            "stop_loss": round(price * 0.95, 2)
                        })
                if len(valid_picks) >= effective_k:
                    break

        if not valid_picks:
            return ""

        # 4. [P2 级] 生成并推送精炼的【Telegram 决策看板速览卡片】
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        regime_desc = regime.get("title", "量化精选")
        market_stats = f"上涨 {regime.get('up_count', 0)} | 下跌 {regime.get('down_count', 0)} | 中位数 {regime.get('median_pct', 0.0):+.2f}%" if regime.get("up_count") else "行情监控就绪"

        dashboard_lines = [
            f"🎯 *【{slot_title} · 核心决策看板】*",
            f"⏰ 时间: `{now_str}` | {regime_desc}",
            f"📊 市场环境: `{market_stats}`",
            "━━━━━━━━━━━━━━━━━━━"
        ]

        # 拼接 P3 历史战绩 (若有)
        if t_plus_one_card:
            dashboard_lines.insert(0, t_plus_one_card)

        for idx, item in enumerate(valid_picks, 1):
            line = (
                f"*{idx}. {item['name']}* (`{item['code']}`)\n"
                f"   • 现价: `{item['price']:.2f}` ({item['pct']:+.2f}%) | 量比: `{item['volume_ratio']:.1f}`\n"
                f"   • 建议建仓: `{item['buy_min']:.2f} ~ {item['buy_max']:.2f}`\n"
                f"   • 止损参考: `{item['stop_loss']:.2f}` (跌破坚决离场)"
            )
            dashboard_lines.append(line)

        dashboard_lines.append("━━━━━━━━━━━━━━━━━━━")
        dashboard_lines.append("💡 *提示*: 上述标的由全市场量化初筛，详细 AI 深度研报稍后由 Gemini 逐一诊断生成！")

        full_message = "\n".join(dashboard_lines)
        send_telegram(full_message)

        # 5. [P3 级] 保存本次推荐到 GitHub 数据库供下期跟踪
        today_date = datetime.now().strftime("%Y-%m-%d")
        new_history = history_db or {"history": []}
        current_record = {
            "date": today_date,
            "slot": time_slot,
            "timestamp": int(time.time()),
            "picks": [{"code": x["code"], "name": x["name"], "price": x["price"]} for x in valid_picks]
        }
        new_history["last_run"] = current_record
        if "history" not in new_history:
            new_history["history"] = []
        new_history["history"].append(current_record)
        # 只保留最近 30 期
        new_history["history"] = new_history["history"][-30:]

        save_history_to_github(gh_token, new_history, history_sha)

        # 6. 返回股票代码列表给 main.py 进行 Gemini AI 研报诊断
        result_codes = [x["code"] for x in valid_picks]
        return ",".join(result_codes)

    except Exception as e:
        logger.error(f"量化全流程异常: {e}")
        return ""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="量化智能荐股与闭环风控引擎")
    parser.add_argument("--time-slot", type=str, default="morning", choices=["morning", "afternoon"])
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()

    codes = pick_stocks(time_slot=args.time_slot, top_k=args.top_k)
    print(codes)
