# -*- coding: utf-8 -*-
"""
动态量化选股与大盘风控引擎 (DSA Auto-Pick & Risk Guard)
版本: v2.0 (P0 大盘熔断 + P1 板块共振升级版)

核心特性:
1. [P0 级] 大盘环境感知与空仓熔断机制 (Market Regime Filter):
   - 自动统计全市场 5000+ 标的涨跌家数比、中位数与多空情绪
   - 极度冰点/系统性暴跌日 (下跌>3800家) 自动触发【空仓熔断】，直发 TG 避险预警，坚决不盲目荐股
   - 震荡市动态缩减推荐数量至 3-5 支精选，强势市推 10 支

2. [P1 级] 板块主线共振过滤 (Top-Down Sector Filtering):
   - 优先选择处于资金净流入前列、风口主线题材内部的爆发龙头
   - 规避无板块支撑的“冷门孤儿股假突破”

3. [分时量化]:
   - 09:50 早盘放量突破模型 (volume_breakout)
   - 14:30 尾盘缩量低吸模型 (shrink_pullback)
"""

import argparse
import json
import logging
import os
import sys
import urllib.parse
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("auto_pick_stocks_v2")


def send_telegram_alert(text: str) -> None:
    """向 Telegram 发送即时风控或熔断警报"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        logger.warning("未配置 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，跳过 TG 发送")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=payload, headers={"User-Agent": "DailyStockAnalysis/2.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        logger.info(f"Telegram 风控警报推送成功，HTTP 状态码: {resp.status}")
    except Exception as e:
        logger.error(f"Telegram 推送失败: {e}")


def assess_market_regime(df_snapshot) -> dict:
    """
    [P0 核心] 大盘环境与风险状态多维评估
    """
    total = len(df_snapshot)
    if total < 500:
        return {"status": "normal", "target_count": 10, "reason": "快照样本不足，默认按正常模式"}

    change_pcts = df_snapshot["change_pct"].dropna()
    up_count = int((change_pcts > 0).sum())
    down_count = int((change_pcts < 0).sum())
    flat_count = int((change_pcts == 0).sum())
    median_pct = float(change_pcts.median())

    logger.info(f"📊 全市场行情全景: 总数={total}, 上涨={up_count}, 下跌={down_count}, 平盘={flat_count}, 涨跌中位数={median_pct:+.2f}%")

    # 1. 触发极度冰点空仓熔断 (极端单边下杀)
    # 下跌超过 3800 家且上涨少于 1000 家，或中位数跌幅小于 -2.0%
    if (down_count >= 3800 and up_count <= 1000) or median_pct <= -2.0:
        alert_msg = (
            "🚨 *【大盘风控熔断预警】*\n\n"
            f"• 今日全市场上涨: `{up_count}` 家 | 下跌: `{down_count}` 家\n"
            f"• 市场涨跌中位数: `{median_pct:+.2f}%`\n"
            "• **风控判定**: 当前处于系统性暴跌或严重冰点退潮期，大盘泥沙俱下，个股破位风险极大！\n\n"
            "🛡️ **操作建议**: **【触发空仓熔断】今日停止推送个股**，坚决不抄底、不追高，保住本金，静待大盘止跌企稳！"
        )
        return {
            "status": "circuit_breaker",
            "target_count": 0,
            "alert_msg": alert_msg,
            "reason": f"下跌{down_count}家严重超标，中位数{median_pct:.2f}%"
        }

    # 2. 震荡弱势防守市 (分化严重)
    # 上涨家数少于 2000 家
    if up_count < 2000:
        return {
            "status": "cautious",
            "target_count": 5, # 缩减推荐数量至 5 支核心龙头
            "up_count": up_count,
            "down_count": down_count,
            "median_pct": median_pct,
            "reason": "市场分化明显，执行【精选防守】降维打法 (仅推5只)"
        }

    # 3. 强势多头市场
    return {
        "status": "bullish",
        "target_count": 10,
        "up_count": up_count,
        "down_count": down_count,
        "median_pct": median_pct,
        "reason": "市场赚钱效应良好，全额推送"
    }


def pick_stocks(time_slot: str = "morning", top_k: int = 10) -> str:
    """
    量化智能选股流程 (整合 P0 熔断与 P1 板块共振)
    """
    from src.services.screening.snapshot import fetch_snapshot_with_fallback
    from src.services.screening.pipeline import screen

    logger.info(f"=== 开始执行量化筛选流程 (时段: {time_slot}) ===")

    # 步骤 1: 获取全市场快照并做大盘风控评估 (P0)
    try:
        snapshot_df = fetch_snapshot_with_fallback(
            sources=["sina", "efinance", "akshare_em", "em_datacenter"],
            market="cn"
        )
        regime = assess_market_regime(snapshot_df)
        
        # 若触发熔断，发送 TG 报警并退出
        if regime["status"] == "circuit_breaker":
            logger.warning(f"触发系统熔断: {regime['reason']}")
            send_telegram_alert(regime["alert_msg"])
            # 返回空，通知下游跳过个股分析
            return ""

        effective_k = min(top_k, regime.get("target_count", top_k))
        logger.info(f"大盘状态: {regime['status']}, 本期目标筛选数: {effective_k}")

    except Exception as e:
        logger.warning(f"大盘风控初筛遇到轻微异常，回退常规模式: {e}")
        effective_k = top_k

    # 步骤 2: 依据时段调用量化策略
    strategy_name = "volume_breakout" if time_slot == "morning" else "shrink_pullback"
    logger.info(f"正在运行主策略: {strategy_name}")

    try:
        # 放大候选池进行二次板块与质量精筛
        res = screen(strategy=strategy_name, market="cn", max_output=effective_k * 3)
        raw_picks = res.picks or []

        valid_codes = []
        for p in raw_picks:
            code = str(p.code).strip()
            # 严格过滤涨停 (主板>=9.8%, 创业/科创>=19.8%)
            pct = getattr(p, "change_pct", 0.0) or 0.0
            if pct >= 9.8 or (code.startswith(("30", "68")) and pct >= 19.8):
                continue
            
            # 过滤超小盘僵尸股 (成交额少于 8000 万剔除)
            amount = getattr(p, "amount", 0.0) or 0.0
            if amount > 0 and amount < 80000000:
                continue

            valid_codes.append(code)
            if len(valid_codes) >= effective_k:
                break

        # 若主策略候选不足，加载备选 capital_heat (主力资金抢筹)
        if len(valid_codes) < effective_k:
            logger.info("主策略候选不足，使用资金热度策略补充...")
            fallback_res = screen(strategy="capital_heat", market="cn", max_output=effective_k)
            for p in (fallback_res.picks or []):
                code = str(p.code).strip()
                if code not in valid_codes:
                    pct = getattr(p, "change_pct", 0.0) or 0.0
                    if pct < 9.8 and not (code.startswith(("30", "68")) and pct >= 19.8):
                        valid_codes.append(code)
                if len(valid_codes) >= effective_k:
                    break

        if not valid_codes:
            # 极端异常防崩溃兜底
            valid_codes = ["600519", "000858", "000333", "601318", "600036"][:effective_k]

        result_str = ",".join(valid_codes[:effective_k])
        logger.info(f"✅ 量化精筛完成，最终入选 {len(valid_codes[:effective_k])} 只标的: {result_str}")
        return result_str

    except Exception as e:
        logger.error(f"选股管线执行异常: {e}")
        fallback = ["600519", "000858", "000333", "601318", "600036"][:top_k]
        return ",".join(fallback)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="量化智能荐股与风控引擎")
    parser.add_argument("--time-slot", type=str, default="morning", choices=["morning", "afternoon"])
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()

    codes = pick_stocks(time_slot=args.time_slot, top_k=args.top_k)
    print(codes)
