#!/usr/bin/env python3
"""
回测参数优化器
网格搜索最优参数组合：
- signal_threshold: 开仓阈值
- sl_atr_mult: 止损ATR倍数
- tp1_atr_mult: 止盈ATR倍数
- max_hold_bars: 最大持仓时间
- 趋势过滤开关
"""
import sys
sys.path.insert(0, '/root/h_v3')

import json
import time
from h_v3_backtest import (
    fetch_candles, calc_rsi, calc_ema, calc_macd,
    calc_bollinger, calc_atr, calc_supertrend, calc_cmf,
    score_bar
)

# ============================================================
# 参数网格
# ============================================================

PARAM_GRID = {
    "signal_threshold": [2.5, 3.0, 3.5, 4.0],
    "sl_atr_mult": [1.5, 2.0, 2.5],
    "tp1_atr_mult": [2.0, 2.5, 3.0],
    "max_hold_bars": [15, 20, 30],
    "trend_filter": [False, True],  # EMA20方向一致才开仓
}

# ============================================================
# 单次回测（内联版，避免重复拉数据）
# ============================================================

def run_backtest_with_params(candles, closes, highs, lows, vols,
                              rsi, ema5, ema20, dif, dea, hist,
                              bb_upper, bb_middle, bb_lower,
                              atr, supertrend_dir, cmf,
                              params: dict) -> dict:
    """用指定参数运行回测，返回绩效"""
    signal_threshold = params["signal_threshold"]
    sl_atr_mult = params["sl_atr_mult"]
    tp1_atr_mult = params["tp1_atr_mult"]
    max_hold_bars = params["max_hold_bars"]
    trend_filter = params["trend_filter"]
    leverage = 5
    position_size = 0.1

    trades = []
    in_position = False
    position = None
    start_bar = 30

    for i in range(start_bar, len(candles) - 1):
        if in_position and position:
            exit_price = None
            exit_reason = None

            if position["direction"] == "long":
                if lows[i] <= position["sl"]:
                    exit_price = position["sl"]
                    exit_reason = "sl"
                elif highs[i] >= position["tp1"]:
                    exit_price = position["tp1"]
                    exit_reason = "tp"
            else:
                if highs[i] >= position["sl"]:
                    exit_price = position["sl"]
                    exit_reason = "sl"
                elif lows[i] <= position["tp1"]:
                    exit_price = position["tp1"]
                    exit_reason = "tp"

            bars_held = i - position["bar_idx"]
            if not exit_price and bars_held >= max_hold_bars:
                exit_price = closes[i]
                exit_reason = "timeout"

            if exit_price:
                if position["direction"] == "long":
                    pnl_pct = (exit_price - position["entry"]) / position["entry"] * 100
                else:
                    pnl_pct = (position["entry"] - exit_price) / position["entry"] * 100
                pnl_pct *= leverage
                trades.append({"pnl_pct": pnl_pct, "direction": position["direction"],
                              "bars_held": bars_held, "reason": exit_reason})
                in_position = False
                position = None
            continue

        # 计算信号
        signal_score = score_bar(
            i, closes, highs, lows, vols,
            rsi, ema5, ema20, dif, dea, hist,
            bb_upper, bb_middle, bb_lower,
            atr, supertrend_dir, cmf
        )

        if abs(signal_score) >= signal_threshold and atr[i] is not None and atr[i] > 0:
            direction = "long" if signal_score > 0 else "short"

            # 趋势过滤
            if trend_filter and ema20[i] is not None and i > 0 and ema20[i-1] is not None:
                if direction == "long" and ema20[i] < ema20[i-1]:
                    continue  # 大趋势向下，不做多
                if direction == "short" and ema20[i] > ema20[i-1]:
                    continue  # 大趋势向上，不做空

            entry_price = closes[i]
            if direction == "long":
                sl = entry_price - atr[i] * sl_atr_mult
                tp1 = entry_price + atr[i] * tp1_atr_mult
            else:
                sl = entry_price + atr[i] * sl_atr_mult
                tp1 = entry_price - atr[i] * tp1_atr_mult

            position = {"direction": direction, "entry": entry_price,
                       "sl": sl, "tp1": tp1, "bar_idx": i}
            in_position = True

    # 计算绩效
    if not trades:
        return {"trades": 0, "win_rate": 0, "pf": 0, "return": 0, "mdd": 0, "sharpe": 0}

    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    win_rate = len(wins) / len(trades)

    total_profit = sum(t["pnl_pct"] for t in wins) if wins else 0
    total_loss = abs(sum(t["pnl_pct"] for t in losses)) if losses else 0.001
    pf = total_profit / total_loss if total_loss > 0 else 0

    # 总收益
    equity = 100.0
    equity_curve = [100.0]
    for t in trades:
        trade_return = t["pnl_pct"] * position_size
        equity *= (1 + trade_return / 100)
        equity_curve.append(equity)
    total_return = (equity - 100)

    # 最大回撤
    peak = equity_curve[0]
    max_dd = 0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Sharpe
    returns = [t["pnl_pct"] * position_size for t in trades]
    avg_ret = sum(returns) / len(returns)
    variance = sum((r - avg_ret) ** 2 for r in returns) / max(len(returns) - 1, 1)
    std_ret = variance ** 0.5
    avg_hold = sum(t["bars_held"] for t in trades) / len(trades)
    trades_per_year = 2190 / max(avg_hold, 1)
    sharpe = (avg_ret / std_ret) * (trades_per_year ** 0.5) if std_ret > 0 else 0

    return {
        "trades": len(trades),
        "win_rate": round(win_rate, 3),
        "pf": round(pf, 2),
        "return": round(total_return, 2),
        "mdd": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "avg_hold": round(avg_hold, 1),
    }


# ============================================================
# 主程序
# ============================================================

def main():
    symbols = ["BTC", "ETH", "SOL"]
    
    # 拉取数据（只拉一次）
    data = {}
    for sym in symbols:
        print(f"Fetching {sym} candles...")
        candles = fetch_candles(sym, "4H", 600)
        if len(candles) < 50:
            print(f"  Skip {sym}: only {len(candles)} candles")
            continue
        
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        vols = [c["vol"] for c in candles]
        
        rsi = calc_rsi(closes, 14)
        ema5 = calc_ema(closes, 5)
        ema20 = calc_ema(closes, 20)
        dif, dea, hist = calc_macd(closes)
        bb_upper, bb_middle, bb_lower = calc_bollinger(closes, 20)
        atr_vals = calc_atr(highs, lows, closes, 14)
        supertrend_dir = calc_supertrend(highs, lows, closes)
        cmf_vals = calc_cmf(highs, lows, closes, vols, 20)
        
        data[sym] = {
            "candles": candles, "closes": closes, "highs": highs,
            "lows": lows, "vols": vols, "rsi": rsi, "ema5": ema5,
            "ema20": ema20, "dif": dif, "dea": dea, "hist": hist,
            "bb_upper": bb_upper, "bb_middle": bb_middle, "bb_lower": bb_lower,
            "atr": atr_vals, "supertrend_dir": supertrend_dir, "cmf": cmf_vals,
        }
    
    # 网格搜索
    print(f"\n{'='*70}")
    print("参数优化 - 网格搜索")
    print(f"{'='*70}")
    
    results = []
    total_combos = (len(PARAM_GRID["signal_threshold"]) *
                    len(PARAM_GRID["sl_atr_mult"]) *
                    len(PARAM_GRID["tp1_atr_mult"]) *
                    len(PARAM_GRID["max_hold_bars"]) *
                    len(PARAM_GRID["trend_filter"]))
    print(f"总组合数: {total_combos}")
    
    combo_idx = 0
    for threshold in PARAM_GRID["signal_threshold"]:
        for sl in PARAM_GRID["sl_atr_mult"]:
            for tp in PARAM_GRID["tp1_atr_mult"]:
                for hold in PARAM_GRID["max_hold_bars"]:
                    for tf in PARAM_GRID["trend_filter"]:
                        combo_idx += 1
                        params = {
                            "signal_threshold": threshold,
                            "sl_atr_mult": sl,
                            "tp1_atr_mult": tp,
                            "max_hold_bars": hold,
                            "trend_filter": tf,
                        }
                        
                        # 对所有币种运行回测
                        combo_results = {}
                        total_return = 0
                        total_mdd = 0
                        total_sharpe = 0
                        valid = 0
                        
                        for sym, d in data.items():
                            r = run_backtest_with_params(
                                d["candles"], d["closes"], d["highs"],
                                d["lows"], d["vols"], d["rsi"], d["ema5"],
                                d["ema20"], d["dif"], d["dea"], d["hist"],
                                d["bb_upper"], d["bb_middle"], d["bb_lower"],
                                d["atr"], d["supertrend_dir"], d["cmf"],
                                params
                            )
                            combo_results[sym] = r
                            if r["trades"] >= 5:
                                total_return += r["return"]
                                total_mdd += r["mdd"]
                                total_sharpe += r["sharpe"]
                                valid += 1
                        
                        if valid > 0:
                            avg_return = total_return / valid
                            avg_mdd = total_mdd / valid
                            avg_sharpe = total_sharpe / valid
                            # 综合评分：收益 - 回撤 + Sharpe*5
                            score = avg_return - avg_mdd * 0.5 + avg_sharpe * 3
                        else:
                            score = -999
                        
                        results.append({
                            "params": params,
                            "details": combo_results,
                            "avg_return": round(total_return / max(valid, 1), 2),
                            "avg_mdd": round(total_mdd / max(valid, 1), 2),
                            "avg_sharpe": round(total_sharpe / max(valid, 1), 2),
                            "score": round(score, 2),
                        })
    
    # 排序输出Top 10
    results.sort(key=lambda x: x["score"], reverse=True)
    
    print(f"\n{'='*70}")
    print("TOP 10 参数组合")
    print(f"{'='*70}")
    print(f"{'#':<3} {'阈值':<5} {'止损':<5} {'止盈':<5} {'持仓':<5} {'趋势':<5} {'收益%':<8} {'回撤%':<8} {'Sharpe':<8} {'评分':<8}")
    print("-" * 70)
    
    for idx, r in enumerate(results[:10]):
        p = r["params"]
        print(f"{idx+1:<3} {p['signal_threshold']:<5} {p['sl_atr_mult']:<5} "
              f"{p['tp1_atr_mult']:<5} {p['max_hold_bars']:<5} "
              f"{'Y' if p['trend_filter'] else 'N':<5} "
              f"{r['avg_return']:<8} {r['avg_mdd']:<8} "
              f"{r['avg_sharpe']:<8} {r['score']:<8}")
    
    # 最优参数详细结果
    best = results[0]
    print(f"\n{'='*70}")
    print(f"最优参数: {best['params']}")
    print(f"{'='*70}")
    for sym, r in best["details"].items():
        print(f"  {sym}: {r['trades']}笔 | 胜率{r['win_rate']*100:.0f}% | "
              f"盈亏比{r['pf']:.2f} | 收益{r['return']:.1f}% | "
              f"回撤{r['mdd']:.1f}% | Sharpe{r['sharpe']:.2f}")
    
    # 保存结果
    with open('/root/h_v3/optimize_results.json', 'w') as f:
        json.dump(results[:20], f, indent=2)
    print("\n✓ 优化结果已保存到 optimize_results.json")


if __name__ == "__main__":
    main()
