# -*- coding: utf-8 -*-
"""
动态量化选股引擎 (AI 智能荐股模块)
根据当前时段（早盘/尾盘）调用内置量化策略从 A 股全市场筛选出 10 支潜力个股。
"""

import argparse
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("auto_pick_stocks")


def pick_stocks(time_slot: str = "morning", top_k: int = 10) -> str:
    """
    根据时段选择策略：
    - morning (09:50): volume_breakout (放量突破) / capital_heat (主力抢筹)
    - afternoon (14:30): shrink_pullback (缩量回踩) / momentum_quality (稳健多头)
    """
    from src.services.screening.pipeline import screen
    
    # 策略路由
    strategy_name = "volume_breakout" if time_slot == "morning" else "shrink_pullback"
    logger.info(f"正在执行【{time_slot}】选股策略: {strategy_name}, 目标数量: {top_k}")
    
    try:
        res = screen(strategy=strategy_name, market="cn", max_output=top_k * 2)
        picks = res.picks or []
        
        valid_codes = []
        for p in picks:
            code = str(p.code).strip()
            pct = getattr(p, "change_pct", 0.0) or 0.0
            if pct >= 9.8 or (code.startswith(("30", "68")) and pct >= 19.8):
                continue
            valid_codes.append(code)
            if len(valid_codes) >= top_k:
                break
                
        if len(valid_codes) < top_k:
            logger.info("第一策略候选不足，加载备选策略 capital_heat 补充...")
            fallback_res = screen(strategy="capital_heat", market="cn", max_output=top_k)
            for p in (fallback_res.picks or []):
                code = str(p.code).strip()
                if code not in valid_codes:
                    valid_codes.append(code)
                if len(valid_codes) >= top_k:
                    break

        if not valid_codes:
            valid_codes = ["600519", "000858", "000333", "601318", "600036", 
                           "002594", "300750", "688981", "601899", "600900"][:top_k]

        result_str = ",".join(valid_codes[:top_k])
        logger.info(f"✅ 成功筛选出 {len(valid_codes[:top_k])} 支推荐股票: {result_str}")
        return result_str

    except Exception as e:
        logger.error(f"选股策略执行异常: {e}，启用高流动性核心龙头兜底")
        fallback = ["600519", "000858", "000333", "601318", "600036", 
                    "002594", "300750", "688981", "601899", "600900"][:top_k]
        return ",".join(fallback)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="智能荐股选股脚本")
    parser.add_argument("--time-slot", type=str, default="morning", choices=["morning", "afternoon"],
                        help="运行时间段: morning (早盘) 或 afternoon (尾盘)")
    parser.add_argument("--top-k", type=int, default=10, help="推荐股票只数")
    args = parser.parse_args()

    codes = pick_stocks(time_slot=args.time_slot, top_k=args.top_k)
    print(codes)
