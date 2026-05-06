"""
H_V3 MCP Backtest Server - AI 回测验证
==========================================
基于 MCP 协议封装 AI 回测验证引擎。
提供标准化的策略回测、参数优化、绩效分析服务。
任何 MCP Client 均可无缝调用本服务进行策略验证。

支持的 Tools:
  - run_backtest: 运行单次策略回测
  - optimize_params: 网格搜索参数优化
  - get_performance: 获取策略绩效指标
  - compare_strategies: 对比多策略表现

依赖: vectorbtpro, numpy, pandas
MCP 传输: stdio
"""

import json
import time
import math
import urllib.request
import urllib.error
from typing import Any
from mcp.server.fastmcp import FastMCP

try:
    import numpy as np
    import pandas as pd
    import vectorbtpro as vbt
    VBT_AVAILABLE = True
except ImportError:
    VBT_AVAILABLE = False

# ============================================================
# MCP Server 初始化
# ============================================================

mcp = FastMCP("H_V3 AI Backtest")

# ============================================================
# 配置常量
# ============================================================

OKX_BASE_URL = "https://www.okx.com"

# 默认回测参数
DEFAULT_INIT_CASH = 10000       # 初始资金 $10,000
DEFAULT_FEES = 0.0006           # OKX maker 手续费 0.06%
DEFAULT_SLIPPAGE = 0.0005       # 滑点 0.05%
DEFAULT_TIMEFRAME = "1H"        # 默认 1H K线
DEFAULT_DAYS = 180              # 默认回测 180 天

# 交易对映射
INSTRUMENT_MAP = {
    "BTC": "BTC-USDT-SWAP",
    "ETH": "ETH-USDT-SWAP",
    "SOL": "SOL-USDT-SWAP",
    "DOGE": "DOGE-USDT-SWAP",
    "OKB": "OKB-USDT-SWAP",
}


# ============================================================
# 内部工具函数
# ============================================================

def _fetch_history_candles(inst_id: str, bar: str = "1H", days: int = 180) -> list:
    """从 OKX V5 获取历史 K 线数据（自动分页）"""
    all_candles = []
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - days * 24 * 3600 * 1000

    after = ""
    max_retries = 3

    while True:
        url = f"{OKX_BASE_URL}/api/v5/market/history-candles?instId={inst_id}&bar={bar}&limit=100"
        if after:
            url += f"&after={after}"

        req = urllib.request.Request(url, headers={"User-Agent": "H_V3/3.0.0"})

        for attempt in range(max_retries):
            try:
                resp = urllib.request.urlopen(req, timeout=15)
                data = json.loads(resp.read())
                candles = data.get("data", [])
                break
            except Exception:
                if attempt == max_retries - 1:
                    candles = []
                time.sleep(1)

        if not candles:
            break

        all_candles.extend(candles)
        oldest_ts = int(candles[-1][0])
        if oldest_ts <= start_ts:
            break
        after = candles[-1][0]
        time.sleep(0.1)  # Rate limit

    return all_candles


def _candles_to_dataframe(raw_candles: list) -> "pd.DataFrame":
    """将原始 K 线数据转换为 pandas DataFrame"""
    if not VBT_AVAILABLE:
        return None

    df = pd.DataFrame(raw_candles, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'
    ])
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
    for col in ['open', 'high', 'low', 'close', 'vol']:
        df[col] = df[col].astype(float)
    df = df.sort_values('timestamp').reset_index(drop=True)
    df = df.set_index('timestamp')
    return df


def _calc_rsi(series, period=14):
    """计算 RSI (Wilder 法)"""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _calc_ema(series, period):
    """计算 EMA"""
    return series.ewm(span=period, adjust=False).mean()


def _calc_macd(series, fast=12, slow=26, signal=9):
    """计算 MACD"""
    ema_fast = _calc_ema(series, fast)
    ema_slow = _calc_ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _calc_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _calc_atr(high, low, close, period=14):
    """计算 ATR"""
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _calc_hurst(series, window=100):
    """计算赫斯特指数（滚动窗口）"""
    hurst_values = pd.Series(index=series.index, dtype=float)
    values = series.values
    for i in range(window, len(values)):
        ts = values[i-window:i]
        mean_val = np.mean(ts)
        deviations = ts - mean_val
        cumulative = np.cumsum(deviations)
        R = np.max(cumulative) - np.min(cumulative)
        S = np.std(ts, ddof=1)
        if S > 0 and R > 0:
            hurst_values.iloc[i] = np.log(R/S) / np.log(window)
        else:
            hurst_values.iloc[i] = 0.5
    return hurst_values


def _generate_signals(close, rsi, ema_fast, ema_slow, macd_hist, hurst,
                      rsi_buy=35, rsi_sell=70, hurst_threshold=0.5):
    """
    多因子信号生成器：
    做多条件：RSI超卖 + EMA金叉/价格在EMA上方 + MACD正 + 赫斯特确认趋势
    平仓条件：RSI超买 或 趋势反转
    """
    entries = pd.Series(False, index=close.index)
    exits = pd.Series(False, index=close.index)

    for i in range(1, len(close)):
        # 赫斯特过滤：只在趋势市交易
        if pd.isna(hurst.iloc[i]) or hurst.iloc[i] < hurst_threshold:
            continue

        # 做多条件
        rsi_ok = rsi.iloc[i] < rsi_buy
        ema_ok = ema_fast.iloc[i] > ema_slow.iloc[i] or close.iloc[i] > ema_fast.iloc[i]
        macd_ok = macd_hist.iloc[i] > 0 or (macd_hist.iloc[i] > macd_hist.iloc[i-1])

        if rsi_ok and ema_ok and macd_ok:
            entries.iloc[i] = True

        # 平仓条件
        rsi_exit = rsi.iloc[i] > rsi_sell
        ema_exit = ema_fast.iloc[i] < ema_slow.iloc[i] and close.iloc[i] < ema_fast.iloc[i]

        if rsi_exit or ema_exit:
            exits.iloc[i] = True

    return entries, exits


def _run_single_backtest(df, rsi_buy=35, rsi_sell=70, hurst_threshold=0.5,
                         init_cash=DEFAULT_INIT_CASH, fees=DEFAULT_FEES,
                         slippage=DEFAULT_SLIPPAGE, direction="longonly"):
    """运行单次回测，返回绩效数据"""
    close = df['close']
    high = df['high']
    low = df['low']

    # 计算指标
    rsi = _calc_rsi(close, 14)
    ema_fast = _calc_ema(close, 20)
    ema_slow = _calc_ema(close, 50)
    _, _, macd_hist = _calc_macd(close)
    hurst = _calc_hurst(close, 100)

    # 生成信号
    entries, exits = _generate_signals(
        close, rsi, ema_fast, ema_slow, macd_hist, hurst,
        rsi_buy, rsi_sell, hurst_threshold
    )

    if entries.sum() == 0:
        return {"error": True, "message": "无有效交易信号"}

    # VBT Pro 回测
    pf = vbt.Portfolio.from_signals(
        close=close,
        entries=entries,
        exits=exits,
        init_cash=init_cash,
        fees=fees,
        slippage=slippage,
        freq="1h",
        direction=direction
    )

    # 提取绩效
    total_return = float(pf.total_return * 100)
    max_drawdown = float(pf.max_drawdown * 100)
    sharpe = float(pf.sharpe_ratio) if not np.isnan(pf.sharpe_ratio) else 0
    trade_count = int(pf.trades.count())
    win_rate = float(pf.trades.win_rate * 100) if trade_count > 0 else 0
    buy_hold = float((close.iloc[-1] / close.iloc[0] - 1) * 100)

    # Sortino ratio
    try:
        sortino = float(pf.sortino_ratio) if not np.isnan(pf.sortino_ratio) else 0
    except:
        sortino = 0

    # Profit factor
    try:
        profit_factor = float(pf.trades.profit_factor) if trade_count > 0 else 0
    except:
        profit_factor = 0

    return {
        "error": False,
        "initial_cash": init_cash,
        "final_value": round(float(pf.final_value), 2),
        "total_return_pct": round(total_return, 2),
        "buy_hold_return_pct": round(buy_hold, 2),
        "alpha_pct": round(total_return - buy_hold, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "sharpe_ratio": round(sharpe, 3),
        "sortino_ratio": round(sortino, 3),
        "profit_factor": round(profit_factor, 2),
        "total_trades": trade_count,
        "win_rate_pct": round(win_rate, 1),
        "entry_signals": int(entries.sum()),
        "exit_signals": int(exits.sum()),
        "params": {
            "rsi_buy": rsi_buy,
            "rsi_sell": rsi_sell,
            "hurst_threshold": hurst_threshold,
        },
    }


# ============================================================
# MCP Tools 定义
# ============================================================

@mcp.tool()
def run_backtest(
    symbol: str = "BTC",
    timeframe: str = "1H",
    days: int = 180,
    rsi_buy: int = 35,
    rsi_sell: int = 70,
    hurst_threshold: float = 0.5,
    init_cash: float = 10000,
    direction: str = "longonly"
) -> dict:
    """
    运行单次策略回测。

    Args:
        symbol: 交易对符号（BTC/ETH/SOL/DOGE/OKB）
        timeframe: K线周期（1m/5m/15m/1H/4H/1D）
        days: 回测天数（30-365）
        rsi_buy: RSI 买入阈值（低于此值触发买入）
        rsi_sell: RSI 卖出阈值（高于此值触发卖出）
        hurst_threshold: 赫斯特指数阈值（高于此值才交易）
        init_cash: 初始资金（USDT）
        direction: 交易方向（longonly/shortonly/both）

    Returns:
        完整的回测绩效报告，包含收益率、最大回撤、夏普比率、胜率等
    """
    if not VBT_AVAILABLE:
        return {"error": True, "message": "AI 回测验证引擎未就绪"}

    # 解析交易对
    symbol_upper = symbol.upper().strip()
    inst_id = INSTRUMENT_MAP.get(symbol_upper, f"{symbol_upper}-USDT-SWAP")

    # 获取历史数据
    raw_candles = _fetch_history_candles(inst_id, timeframe, days)
    if not raw_candles or len(raw_candles) < 200:
        return {"error": True, "message": f"获取 {symbol} 历史数据失败或数据不足（需要至少200根K线）"}

    # 转换为 DataFrame
    df = _candles_to_dataframe(raw_candles)
    if df is None or len(df) < 200:
        return {"error": True, "message": "数据转换失败"}

    # 运行回测
    result = _run_single_backtest(
        df, rsi_buy, rsi_sell, hurst_threshold,
        init_cash, DEFAULT_FEES, DEFAULT_SLIPPAGE, direction
    )

    # 补充元信息
    if not result.get("error"):
        result["symbol"] = symbol_upper
        result["inst_id"] = inst_id
        result["timeframe"] = timeframe
        result["period_days"] = days
        result["period_start"] = str(df.index[0])
        result["period_end"] = str(df.index[-1])
        result["candle_count"] = len(df)
        result["strategy"] = "RSI + EMA(20/50) + MACD + Hurst Filter"

    return result


@mcp.tool()
def optimize_params(
    symbol: str = "BTC",
    timeframe: str = "1H",
    days: int = 180,
    rsi_buy_range: list[int] = None,
    rsi_sell_range: list[int] = None,
    hurst_range: list[float] = None,
    optimize_target: str = "sharpe"
) -> dict:
    """
    网格搜索参数优化，找到最优参数组合。

    Args:
        symbol: 交易对符号
        timeframe: K线周期
        days: 回测天数
        rsi_buy_range: RSI 买入阈值搜索范围，如 [25, 30, 35, 40]
        rsi_sell_range: RSI 卖出阈值搜索范围，如 [65, 70, 75, 80]
        hurst_range: 赫斯特阈值搜索范围，如 [0.45, 0.50, 0.55, 0.60]
        optimize_target: 优化目标（sharpe/return/drawdown）

    Returns:
        最优参数组合及 TOP 5 结果
    """
    if not VBT_AVAILABLE:
        return {"error": True, "message": "AI 回测验证引擎未就绪"}

    # 默认搜索范围
    if rsi_buy_range is None:
        rsi_buy_range = [25, 30, 35, 40, 45]
    if rsi_sell_range is None:
        rsi_sell_range = [60, 65, 70, 75, 80]
    if hurst_range is None:
        hurst_range = [0.40, 0.45, 0.50, 0.55, 0.60]

    # 获取数据
    symbol_upper = symbol.upper().strip()
    inst_id = INSTRUMENT_MAP.get(symbol_upper, f"{symbol_upper}-USDT-SWAP")
    raw_candles = _fetch_history_candles(inst_id, timeframe, days)

    if not raw_candles or len(raw_candles) < 200:
        return {"error": True, "message": f"获取 {symbol} 历史数据失败"}

    df = _candles_to_dataframe(raw_candles)
    if df is None or len(df) < 200:
        return {"error": True, "message": "数据不足"}

    # 网格搜索
    results = []
    total_combos = len(rsi_buy_range) * len(rsi_sell_range) * len(hurst_range)

    for rb in rsi_buy_range:
        for rs in rsi_sell_range:
            if rb >= rs:  # 买入阈值必须小于卖出阈值
                continue
            for ht in hurst_range:
                result = _run_single_backtest(df, rb, rs, ht)
                if not result.get("error"):
                    results.append(result)

    if not results:
        return {"error": True, "message": "所有参数组合均无有效信号"}

    # 按目标排序
    if optimize_target == "sharpe":
        results.sort(key=lambda x: x["sharpe_ratio"], reverse=True)
    elif optimize_target == "return":
        results.sort(key=lambda x: x["total_return_pct"], reverse=True)
    elif optimize_target == "drawdown":
        results.sort(key=lambda x: abs(x["max_drawdown_pct"]))

    # 提取 TOP 5
    top5 = results[:5]
    best = results[0]

    return {
        "error": False,
        "symbol": symbol_upper,
        "timeframe": timeframe,
        "period_days": days,
        "total_combinations": total_combos,
        "valid_results": len(results),
        "optimize_target": optimize_target,
        "best_params": best["params"],
        "best_performance": {
            "total_return_pct": best["total_return_pct"],
            "max_drawdown_pct": best["max_drawdown_pct"],
            "sharpe_ratio": best["sharpe_ratio"],
            "win_rate_pct": best["win_rate_pct"],
            "total_trades": best["total_trades"],
            "alpha_pct": best["alpha_pct"],
        },
        "top5": [
            {
                "params": r["params"],
                "return_pct": r["total_return_pct"],
                "drawdown_pct": r["max_drawdown_pct"],
                "sharpe": r["sharpe_ratio"],
                "trades": r["total_trades"],
            }
            for r in top5
        ],
    }


@mcp.tool()
def get_performance(
    symbol: str = "BTC",
    timeframe: str = "1H",
    days: int = 90
) -> dict:
    """
    快速获取当前策略（默认参数）的绩效概览。
    适合日常监控策略表现。

    Args:
        symbol: 交易对符号
        timeframe: K线周期
        days: 回测天数

    Returns:
        策略绩效摘要
    """
    result = run_backtest(symbol=symbol, timeframe=timeframe, days=days)

    if result.get("error"):
        return result

    # 绩效评级
    sharpe = result["sharpe_ratio"]
    if sharpe >= 2.0:
        grade = "A+ (优秀)"
    elif sharpe >= 1.5:
        grade = "A (良好)"
    elif sharpe >= 1.0:
        grade = "B (合格)"
    elif sharpe >= 0.5:
        grade = "C (一般)"
    else:
        grade = "D (需优化)"

    # 风险评估
    dd = abs(result["max_drawdown_pct"])
    if dd < 10:
        risk = "低风险"
    elif dd < 20:
        risk = "中等风险"
    elif dd < 30:
        risk = "较高风险"
    else:
        risk = "高风险"

    return {
        "error": False,
        "symbol": result["symbol"],
        "period": f"{result.get('period_start', '')} ~ {result.get('period_end', '')}",
        "strategy_grade": grade,
        "risk_level": risk,
        "total_return_pct": result["total_return_pct"],
        "buy_hold_return_pct": result["buy_hold_return_pct"],
        "alpha_pct": result["alpha_pct"],
        "max_drawdown_pct": result["max_drawdown_pct"],
        "sharpe_ratio": result["sharpe_ratio"],
        "win_rate_pct": result["win_rate_pct"],
        "total_trades": result["total_trades"],
        "recommendation": "策略有效，可继续使用" if sharpe >= 1.0 else "策略表现不佳，建议优化参数或暂停使用",
    }


@mcp.tool()
def compare_strategies(
    symbols: list[str] = None,
    timeframe: str = "1H",
    days: int = 90
) -> dict:
    """
    对比多个币种在同一策略下的表现，找出最佳交易标的。

    Args:
        symbols: 币种列表，如 ["BTC", "ETH", "SOL"]。不传则对比所有已配置币种。
        timeframe: K线周期
        days: 回测天数

    Returns:
        各币种绩效对比及排名
    """
    if symbols is None:
        symbols = list(INSTRUMENT_MAP.keys())

    results = []
    for sym in symbols:
        result = run_backtest(symbol=sym, timeframe=timeframe, days=days)
        if not result.get("error"):
            results.append({
                "symbol": sym,
                "total_return_pct": result["total_return_pct"],
                "alpha_pct": result["alpha_pct"],
                "max_drawdown_pct": result["max_drawdown_pct"],
                "sharpe_ratio": result["sharpe_ratio"],
                "win_rate_pct": result["win_rate_pct"],
                "total_trades": result["total_trades"],
            })

    if not results:
        return {"error": True, "message": "所有币种回测均失败"}

    # 按夏普比率排序
    results.sort(key=lambda x: x["sharpe_ratio"], reverse=True)

    return {
        "error": False,
        "timeframe": timeframe,
        "period_days": days,
        "ranking": results,
        "best_symbol": results[0]["symbol"],
        "best_sharpe": results[0]["sharpe_ratio"],
        "worst_symbol": results[-1]["symbol"],
        "worst_sharpe": results[-1]["sharpe_ratio"],
    }


# ============================================================
# MCP Resources
# ============================================================

@mcp.resource("config://backtest_params")
def get_backtest_config() -> str:
    """返回回测引擎配置"""
    config = {
        "default_init_cash": DEFAULT_INIT_CASH,
        "default_fees": DEFAULT_FEES,
        "default_slippage": DEFAULT_SLIPPAGE,
        "default_timeframe": DEFAULT_TIMEFRAME,
        "default_days": DEFAULT_DAYS,
        "supported_symbols": list(INSTRUMENT_MAP.keys()),
        "strategy": "RSI + EMA(20/50) + MACD + Hurst Filter",
        "engine_version": vbt.__version__ if VBT_AVAILABLE else "NOT INSTALLED",
    }
    return json.dumps(config, indent=2)


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
