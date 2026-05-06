"""
H_V3 MCP Engine Server
========================
基于 MCP 协议封装技术面指标计算引擎。
接收标准化的 K 线数据，输出赫斯特指数、RSI、EMA、MACD、布林带、ATR 等指标，
以及多因子评分和交易信号。

支持的 Tools:
  - calculate_hurst: 计算赫斯特指数（判断趋势/震荡）
  - calculate_indicators: 计算全套技术指标
  - generate_signal: 生成多因子交易信号（含入场/止盈/止损）
  - scan_symbol: 对单个币种做完整扫描（调用 OKX Market Server 获取数据 + 计算信号）

依赖: numpy, pandas
MCP 传输: stdio
"""

import json
import math
import urllib.request
import sys
import os
from typing import Any
from mcp.server.fastmcp import FastMCP

# 导入聪明钱模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from h_v3_mcp_smartmoney import get_smart_money_summary, start_smart_money_service
    SMARTMONEY_AVAILABLE = True
except ImportError:
    SMARTMONEY_AVAILABLE = False
    start_smart_money_service = None
    print("[Engine] 聪明钱模块未加载，将使用纯技术面")

# ============================================================
# MCP Server 初始化
# ============================================================

mcp = FastMCP("H_V3 Engine")

# ============================================================
# 配置常量
# ============================================================

# 赫斯特指数阈值
HURST_TREND_THRESHOLD = 0.6      # > 0.6 为趋势市场
HURST_MEAN_REVERT_THRESHOLD = 0.4  # < 0.4 为均值回归

# 信号评分阈值
SIGNAL_THRESHOLD_LONG = 3    # >= 3 分做多
SIGNAL_THRESHOLD_SHORT = -3  # <= -3 分做空

# ATR 止盈止损倍数
ATR_SL_MULTIPLIER = 1.5   # 止损 = 1.5 * ATR
ATR_TP_MULTIPLIER = 2.5   # 止盈 = 2.5 * ATR

# OKX Market Server 地址（内部调用）
OKX_BASE_URL = "https://www.okx.com"

# 交易对映射
INSTRUMENT_MAP = {
    "BTC": {"inst_id": "BTC-USDT-SWAP", "name": "比特币"},
    "ETH": {"inst_id": "ETH-USDT-SWAP", "name": "以太坊"},
    "SOL": {"inst_id": "SOL-USDT-SWAP", "name": "Solana"},
    "DOGE": {"inst_id": "DOGE-USDT-SWAP", "name": "狗狗币"},
    "OKB": {"inst_id": "OKB-USDT-SWAP", "name": "OKB"},
}


# ============================================================
# 内部计算函数
# ============================================================

def _fetch_klines(inst_id: str, bar: str = "4H", limit: int = 200) -> list:
    """从 OKX V5 获取 K 线数据"""
    url = f"{OKX_BASE_URL}/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "H_V3/3.0.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        if data["code"] == "0" and data["data"]:
            candles = []
            for c in data["data"]:
                candles.append({
                    "timestamp": int(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]),
                })
            candles.reverse()  # 正序：最旧在前
            return candles
    except Exception:
        pass
    return []


def _calc_hurst(closes: list, max_lag: int = 20) -> float:
    """
    R/S 法计算赫斯特指数。
    H > 0.6: 趋势持续
    H ≈ 0.5: 随机游走
    H < 0.4: 均值回归
    """
    n = len(closes)
    if n < max_lag * 2:
        return 0.5

    lags = range(2, max_lag + 1)
    rs_values = []

    for lag in lags:
        rs_list = []
        for start in range(0, n - lag, lag):
            segment = closes[start:start + lag]
            mean_val = sum(segment) / len(segment)
            deviations = [x - mean_val for x in segment]
            cumulative = []
            s = 0
            for d in deviations:
                s += d
                cumulative.append(s)
            r = max(cumulative) - min(cumulative)
            std = (sum((x - mean_val) ** 2 for x in segment) / len(segment)) ** 0.5
            if std > 0:
                rs_list.append(r / std)
        if rs_list:
            rs_values.append((lag, sum(rs_list) / len(rs_list)))

    if len(rs_values) < 3:
        return 0.5

    # 线性回归 log(R/S) vs log(lag) 的斜率即为 H
    log_lags = [math.log(x[0]) for x in rs_values]
    log_rs = [math.log(x[1]) for x in rs_values if x[1] > 0]

    if len(log_rs) != len(log_lags):
        return 0.5

    n_pts = len(log_lags)
    sum_x = sum(log_lags)
    sum_y = sum(log_rs)
    sum_xy = sum(log_lags[i] * log_rs[i] for i in range(n_pts))
    sum_x2 = sum(x ** 2 for x in log_lags)

    denom = n_pts * sum_x2 - sum_x ** 2
    if denom == 0:
        return 0.5

    h = (n_pts * sum_xy - sum_x * sum_y) / denom
    return max(0.0, min(1.0, h))


def _calc_rsi(closes: list, period: int = 14) -> float:
    """计算 RSI"""
    if len(closes) < period + 1:
        return 50.0

    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _calc_ema(values: list, period: int) -> list:
    """计算 EMA 序列"""
    if not values:
        return []
    multiplier = 2 / (period + 1)
    ema = [values[0]]
    for i in range(1, len(values)):
        ema.append(values[i] * multiplier + ema[-1] * (1 - multiplier))
    return ema


def _calc_macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """计算 MACD"""
    if len(closes) < slow + signal:
        return {"macd_line": 0, "signal_line": 0, "histogram": 0}

    ema_fast = _calc_ema(closes, fast)
    ema_slow = _calc_ema(closes, slow)
    macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]
    signal_line = _calc_ema(macd_line[slow:], signal)

    if not signal_line:
        return {"macd_line": 0, "signal_line": 0, "histogram": 0}

    return {
        "macd_line": macd_line[-1],
        "signal_line": signal_line[-1],
        "histogram": macd_line[-1] - signal_line[-1],
    }


def _calc_atr(candles: list, period: int = 14) -> float:
    """计算 ATR"""
    if len(candles) < period + 1:
        return 0.0

    true_ranges = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    return sum(true_ranges[-period:]) / period


def _calc_bollinger(closes: list, period: int = 20, std_dev: float = 2.0) -> dict:
    """计算布林带"""
    if len(closes) < period:
        return {"upper": 0, "middle": 0, "lower": 0, "width": 0}

    recent = closes[-period:]
    middle = sum(recent) / period
    variance = sum((x - middle) ** 2 for x in recent) / period
    std = variance ** 0.5

    upper = middle + std_dev * std
    lower = middle - std_dev * std
    width = (upper - lower) / middle * 100 if middle > 0 else 0

    return {"upper": upper, "middle": middle, "lower": lower, "width": width}


# ============================================================
# MCP Tools 定义
# ============================================================

@mcp.tool()
def calculate_hurst(closes: list[float], max_lag: int = 20) -> dict:
    """
    计算赫斯特指数，判断市场是趋势还是震荡。

    Args:
        closes: 收盘价序列（至少 40 个数据点）
        max_lag: 最大滞后期数，默认 20

    Returns:
        - hurst: 赫斯特指数值 (0-1)
        - market_state: 市场状态描述（强趋势/弱趋势/随机/均值回归）
        - interpretation: 解读说明
    """
    h = _calc_hurst(closes, max_lag)

    if h >= 0.7:
        state = "强趋势"
        interp = "市场具有强烈的趋势持续性，适合趋势跟踪策略"
    elif h >= HURST_TREND_THRESHOLD:
        state = "弱趋势"
        interp = "市场有一定趋势性，可考虑顺势操作"
    elif h >= HURST_MEAN_REVERT_THRESHOLD:
        state = "随机"
        interp = "市场接近随机游走，信号可靠性降低"
    else:
        state = "均值回归"
        interp = "市场倾向于均值回归，适合反转策略"

    return {
        "hurst": round(h, 4),
        "market_state": state,
        "interpretation": interp,
    }


@mcp.tool()
def calculate_indicators(candles: list[dict]) -> dict:
    """
    计算全套技术指标。

    Args:
        candles: K线数据数组，每根包含 {open, high, low, close, volume}

    Returns:
        包含 RSI、EMA、MACD、ATR、布林带等全部指标的字典
    """
    closes = [c["close"] for c in candles]

    # RSI
    rsi = _calc_rsi(closes)

    # EMA
    ema_fast_series = _calc_ema(closes, 12)
    ema_slow_series = _calc_ema(closes, 26)
    ema_fast = ema_fast_series[-1] if ema_fast_series else 0
    ema_slow = ema_slow_series[-1] if ema_slow_series else 0

    # MACD
    macd = _calc_macd(closes)

    # ATR
    atr = _calc_atr(candles)

    # 布林带
    bb = _calc_bollinger(closes)

    # 赫斯特
    hurst_result = calculate_hurst(closes)

    return {
        "rsi": round(rsi, 2),
        "ema_fast": round(ema_fast, 4),
        "ema_slow": round(ema_slow, 4),
        "ema_cross": "golden" if ema_fast > ema_slow else "death",
        "macd_line": round(macd["macd_line"], 4),
        "macd_signal": round(macd["signal_line"], 4),
        "macd_histogram": round(macd["histogram"], 4),
        "atr": round(atr, 4),
        "bollinger_upper": round(bb["upper"], 4),
        "bollinger_middle": round(bb["middle"], 4),
        "bollinger_lower": round(bb["lower"], 4),
        "bollinger_width": round(bb["width"], 2),
        "hurst": hurst_result["hurst"],
        "market_state": hurst_result["market_state"],
        "current_price": closes[-1] if closes else 0,
    }


@mcp.tool()
def generate_signal(candles: list[dict]) -> dict:
    """
    基于多因子模型生成交易信号，包含入场价、止盈价、止损价。

    Args:
        candles: K线数据数组（建议至少 100 根）

    Returns:
        - direction: 方向 (long/short/neutral)
        - score: 综合评分 (-5 到 +5)
        - entry_price: 建议入场价
        - tp_price: 止盈价
        - sl_price: 止损价
        - risk_level: 风险等级 (低/中/高)
        - factors: 各因子详细得分
        - reason: 信号理由说明
    """
    if len(candles) < 30:
        return {"error": True, "message": "K线数据不足，至少需要 30 根"}

    closes = [c["close"] for c in candles]
    current_price = closes[-1]

    # 计算各指标
    rsi = _calc_rsi(closes)
    ema_fast_series = _calc_ema(closes, 12)
    ema_slow_series = _calc_ema(closes, 26)
    ema_fast = ema_fast_series[-1]
    ema_slow = ema_slow_series[-1]
    macd = _calc_macd(closes)
    atr = _calc_atr(candles)
    hurst = _calc_hurst(closes)
    bb = _calc_bollinger(closes)

    # ---- 多因子打分 ----
    long_score = 0.0
    short_score = 0.0
    reasons = []

    # 因子1: EMA 交叉 (权重 1.5)
    if ema_fast > ema_slow:
        long_score += 1.5
        reasons.append("EMA金叉")
    else:
        short_score += 1.5
        reasons.append("EMA死叉")

    # 因子2: RSI (权重 1.0)
    if rsi < 30:
        long_score += 1.0
        reasons.append(f"RSI超卖({rsi:.0f})")
    elif rsi > 70:
        short_score += 1.0
        reasons.append(f"RSI超买({rsi:.0f})")
    elif rsi < 45:
        long_score += 0.5
    elif rsi > 55:
        short_score += 0.5

    # 因子3: MACD 柱状图 (权重 1.0)
    if macd["histogram"] > 0:
        long_score += 1.0
        reasons.append("MACD柱为正")
    else:
        short_score += 1.0
        reasons.append("MACD柱为负")

    # 因子4: 布林带位置 (权重 0.5)
    if bb["lower"] > 0 and current_price < bb["lower"]:
        long_score += 0.5
        reasons.append("价格跌破布林下轨")
    elif bb["upper"] > 0 and current_price > bb["upper"]:
        short_score += 0.5
        reasons.append("价格突破布林上轨")

    # 因子5: 赫斯特趋势确认 (权重 1.0)
    if hurst >= HURST_TREND_THRESHOLD:
        # 趋势市场中，顺势加分
        if ema_fast > ema_slow:
            long_score += 1.0
            reasons.append(f"赫斯特确认趋势(H={hurst:.2f})")
        else:
            short_score += 1.0
            reasons.append(f"赫斯特确认趋势(H={hurst:.2f})")

    # 综合评分
    net_score = long_score - short_score

    # 判断方向
    if net_score >= SIGNAL_THRESHOLD_LONG:
        direction = "long"
    elif net_score <= SIGNAL_THRESHOLD_SHORT:
        direction = "short"
    else:
        direction = "neutral"

    # 计算止盈止损
    if direction == "long":
        entry_price = current_price
        sl_price = current_price - atr * ATR_SL_MULTIPLIER
        tp_price = current_price + atr * ATR_TP_MULTIPLIER
    elif direction == "short":
        entry_price = current_price
        sl_price = current_price + atr * ATR_SL_MULTIPLIER
        tp_price = current_price - atr * ATR_TP_MULTIPLIER
    else:
        entry_price = current_price
        sl_price = 0
        tp_price = 0

    # 风险等级
    if hurst >= 0.7 and abs(net_score) >= 4:
        risk_level = "低"
    elif hurst >= 0.6 and abs(net_score) >= 3:
        risk_level = "中"
    else:
        risk_level = "高"

    # 市场状态
    if hurst >= 0.7:
        market_state = "强趋势"
    elif hurst >= 0.6:
        market_state = "弱趋势"
    elif hurst >= 0.4:
        market_state = "随机"
    else:
        market_state = "均值回归"

    # 信号理由
    if direction == "neutral":
        reason_text = f"市场状态：{market_state}（H={hurst:.3f}），信号强度不足，暂不交易"
    else:
        dir_cn = "做多" if direction == "long" else "做空"
        reason_text = f"建议{dir_cn}：{', '.join(reasons[:3])}"

    return {
        "error": False,
        "direction": direction,
        "score": round(net_score, 1),
        "entry_price": round(entry_price, 4),
        "tp_price": round(tp_price, 4),
        "sl_price": round(sl_price, 4),
        "atr": round(atr, 4),
        "rsi": round(rsi, 2),
        "ema_fast": round(ema_fast, 4),
        "ema_slow": round(ema_slow, 4),
        "macd_histogram": round(macd["histogram"], 4),
        "hurst": round(hurst, 4),
        "market_state": market_state,
        "risk_level": risk_level,
        "reason": reason_text,
        "factors": {
            "long_score": round(long_score, 1),
            "short_score": round(short_score, 1),
            "details": reasons,
        },
    }


@mcp.tool()
def scan_symbol(symbol: str, timeframe: str = "4H") -> dict:
    """
    对单个币种做完整扫描：获取 K 线 → 计算指标 → 聪明钱交叉验证 → 生成信号。
    三维度整合：技术面(70%) + 聪明钱(30%)

    Args:
        symbol: 交易对符号（BTC/ETH/SOL/DOGE/OKB）
        timeframe: K线周期，默认 4H

    Returns:
        完整的扫描结果，包含指标数据、聪明钱数据和交易信号
    """
    symbol_upper = symbol.upper().strip()
    config = INSTRUMENT_MAP.get(symbol_upper)
    if not config:
        return {"error": True, "message": f"不支持的币种: {symbol}"}

    inst_id = config["inst_id"]
    name = config["name"]

    # 获取 K 线数据
    candles = _fetch_klines(inst_id, timeframe, 200)
    if not candles or len(candles) < 30:
        return {"error": True, "message": f"获取 {symbol} K线数据失败或数据不足"}

    # 生成技术面信号
    signal = generate_signal(candles)
    if signal.get("error"):
        return signal

    # ---- 聪明钱维度交叉验证 ----
    smart_money = None
    if SMARTMONEY_AVAILABLE:
        try:
            smart_money = get_smart_money_summary(symbol_upper)
            if smart_money.get("error"):
                smart_money = None
        except Exception as e:
            print(f"[Engine] 聪明钱获取失败 {symbol_upper}: {e}")
            smart_money = None

    # 三维度综合评分调整
    if smart_money and smart_money.get("direction"):
        sm_direction = smart_money["direction"]
        sm_confidence = smart_money.get("confidence", "low")
        tech_direction = signal["direction"]
        tech_score = signal["score"]

        # 聪明钱加分/减分（最大 ±1.5 分）
        sm_bonus = 0.0
        if sm_confidence == "high":
            sm_bonus = 1.5
        elif sm_confidence == "medium":
            sm_bonus = 1.0
        else:
            sm_bonus = 0.5

        # 聪明钱方向与技术面一致 → 加分确认
        # 聪明钱方向与技术面相反 → 减分警告
        if sm_direction == "long":
            if tech_score > 0:  # 技术面也偏多，共振加分
                signal["score"] = round(tech_score + sm_bonus, 1)
            else:  # 技术面偏空但聪明钱做多，减弱空头信号
                signal["score"] = round(tech_score + sm_bonus * 0.5, 1)
        elif sm_direction == "short":
            if tech_score < 0:  # 技术面也偏空，共振加分
                signal["score"] = round(tech_score - sm_bonus, 1)
            else:  # 技术面偏多但聪明钱做空，减弱多头信号
                signal["score"] = round(tech_score - sm_bonus * 0.5, 1)

        # 重新判断方向（基于调整后的分数）
        adjusted_score = signal["score"]
        if adjusted_score >= SIGNAL_THRESHOLD_LONG:
            signal["direction"] = "long"
        elif adjusted_score <= SIGNAL_THRESHOLD_SHORT:
            signal["direction"] = "short"
        else:
            signal["direction"] = "neutral"

        # 重新计算止盈止损（如果方向变了）
        current_price = signal["entry_price"]
        atr = signal["atr"]
        if signal["direction"] == "long":
            signal["sl_price"] = round(current_price - atr * ATR_SL_MULTIPLIER, 4)
            signal["tp_price"] = round(current_price + atr * ATR_TP_MULTIPLIER, 4)
        elif signal["direction"] == "short":
            signal["sl_price"] = round(current_price + atr * ATR_SL_MULTIPLIER, 4)
            signal["tp_price"] = round(current_price - atr * ATR_TP_MULTIPLIER, 4)
        else:
            signal["sl_price"] = 0
            signal["tp_price"] = 0

        # 更新风险等级（聪明钱共振降低风险）
        if sm_direction == signal["direction"] and sm_confidence in ("high", "medium"):
            if signal["risk_level"] == "高":
                signal["risk_level"] = "中"
            elif signal["risk_level"] == "中":
                signal["risk_level"] = "低"

        # 更新理由
        sm_cn = {"long": "做多", "short": "做空", "neutral": "观望"}.get(sm_direction, "")
        conf_cn = {"high": "强", "medium": "中", "low": "弱"}.get(sm_confidence, "")
        if signal["direction"] != "neutral":
            dir_cn = "做多" if signal["direction"] == "long" else "做空"
            signal["reason"] = (
                f"建议{dir_cn}：技术面评分{tech_score:+.1f} + "
                f"聪明钱{conf_cn}共识{sm_cn}({smart_money.get('long_pct', 0):.0f}%多)"
            )

    # 注入聪明钱数据到返回结果
    signal["smart_money"] = {
        "available": smart_money is not None,
        "direction": smart_money.get("direction", "unknown") if smart_money else "unavailable",
        "confidence": smart_money.get("confidence", "low") if smart_money else "none",
        "long_pct": smart_money.get("long_pct", 0) if smart_money else 0,
        "short_pct": smart_money.get("short_pct", 0) if smart_money else 0,
        "summary_text": smart_money.get("summary_text", "数据不可用") if smart_money else "聪明钱数据暂不可用",
    }

    # 补充元信息
    signal["symbol"] = symbol_upper
    signal["inst_id"] = inst_id
    signal["name"] = name
    signal["timeframe"] = timeframe

    return signal


# ============================================================
# MCP Resources
# ============================================================

@mcp.resource("config://engine_params")
def get_engine_params() -> str:
    """返回引擎参数配置"""
    params = {
        "hurst_trend_threshold": HURST_TREND_THRESHOLD,
        "hurst_mean_revert_threshold": HURST_MEAN_REVERT_THRESHOLD,
        "signal_threshold_long": SIGNAL_THRESHOLD_LONG,
        "signal_threshold_short": SIGNAL_THRESHOLD_SHORT,
        "atr_sl_multiplier": ATR_SL_MULTIPLIER,
        "atr_tp_multiplier": ATR_TP_MULTIPLIER,
        "supported_symbols": list(INSTRUMENT_MAP.keys()),
    }
    return json.dumps(params, indent=2)


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
