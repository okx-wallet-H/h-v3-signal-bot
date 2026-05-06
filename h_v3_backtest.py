"""
H V3 回测验证层 (Backtest Layer) v3.3
======================================
四层架构第三层：完整8因子策略回测

核心改进：
1. 拉取100天（600根4H K线）历史数据
2. 在每根K线上计算完整8因子评分
3. 模拟真实开平仓逻辑（ATR止损止盈）
4. 正确计算绩效指标（胜率/盈亏比/回撤/Sharpe）
5. 支持分页拉取突破300根限制

回测逻辑：
- 遍历K线，在每根K线上用技术指标生成信号
- 信号强度≥3.0开仓（方向由评分决定）
- 止损：ATR×1.5 | 止盈1：ATR×2.0 | 止盈2：ATR×3.0
- 最大持仓时间：20根K线（80小时）
- 单次仓位：10%资金
"""

import json
import subprocess
import time
import logging
import os
import math
from typing import Dict, Optional, Any, List, Tuple
from dataclasses import dataclass, field

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [Backtest] %(levelname)s: %(message)s'
)
logger = logging.getLogger("h_v3_backtest")

BACKTEST_CACHE_FILE = "/root/h_v3/backtest_cache.json"
CLI_TIMEOUT = 15


# ============================================================
# 数据结构
# ============================================================

@dataclass
class BacktestResult:
    """回测结果"""
    symbol: str
    timeframe: str
    period_days: int
    total_trades: int
    win_trades: int
    lose_trades: int
    win_rate: float          # 0-1
    profit_factor: float     # 总盈利/总亏损
    avg_win_pct: float       # 平均盈利%
    avg_loss_pct: float      # 平均亏损%
    total_return_pct: float  # 总收益率%
    max_drawdown_pct: float  # 最大回撤%
    sharpe_ratio: float
    avg_hold_bars: float     # 平均持仓K线数
    long_trades: int
    short_trades: int
    long_win_rate: float
    short_win_rate: float
    # 最近交易记录
    last_trades: List[Dict] = field(default_factory=list)
    # 元数据
    timestamp: int = 0
    engine_version: str = "v3.3"

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "period_days": self.period_days,
            "total_trades": self.total_trades,
            "win_trades": self.win_trades,
            "lose_trades": self.lose_trades,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "avg_win_pct": self.avg_win_pct,
            "avg_loss_pct": self.avg_loss_pct,
            "total_return_pct": self.total_return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "avg_hold_bars": self.avg_hold_bars,
            "long_trades": self.long_trades,
            "short_trades": self.short_trades,
            "long_win_rate": self.long_win_rate,
            "short_win_rate": self.short_win_rate,
            "last_trades": self.last_trades[-10:],
            "timestamp": self.timestamp,
            "engine_version": self.engine_version,
        }

    @property
    def summary_text(self) -> str:
        return (
            f"胜率 {self.win_rate*100:.0f}% | "
            f"盈亏比 {self.profit_factor:.2f} | "
            f"回撤 {self.max_drawdown_pct:.1f}% | "
            f"Sharpe {self.sharpe_ratio:.2f}"
        )

    @property
    def detail_text(self) -> str:
        return (
            f"周期: {self.period_days}天 | 交易: {self.total_trades}笔\n"
            f"胜率: {self.win_rate*100:.1f}% ({self.win_trades}胜/{self.lose_trades}负)\n"
            f"盈亏比: {self.profit_factor:.2f} (平均盈{self.avg_win_pct:.2f}%/亏{self.avg_loss_pct:.2f}%)\n"
            f"总收益: {self.total_return_pct:.1f}% | 最大回撤: {self.max_drawdown_pct:.1f}%\n"
            f"Sharpe: {self.sharpe_ratio:.2f} | 平均持仓: {self.avg_hold_bars:.1f}根\n"
            f"多: {self.long_trades}笔({self.long_win_rate*100:.0f}%) | "
            f"空: {self.short_trades}笔({self.short_win_rate*100:.0f}%)"
        )


# ============================================================
# K线数据获取
# ============================================================

def fetch_candles(symbol: str, bar: str = "4H", total: int = 600) -> List[Dict]:
    """
    分页拉取K线数据
    OKX CLI单次最多300根，通过after参数分页拉取更多
    返回按时间升序排列的K线列表
    """
    inst_id = f"{symbol}-USDT-SWAP"
    all_candles = []
    after_ts = None

    pages_needed = math.ceil(total / 300)

    for page in range(pages_needed):
        cmd = f"okx market candles {inst_id} --bar {bar} --limit 300 --json"
        if after_ts:
            cmd += f" --after {after_ts}"

        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=CLI_TIMEOUT
            )
            if result.returncode != 0:
                logger.warning(f"Candles fetch failed page {page}: {result.stderr[:100]}")
                break

            candles = json.loads(result.stdout.strip())
            if not candles:
                break

            all_candles.extend(candles)

            # 最后一根K线的时间戳作为下一页的after
            last_ts = candles[-1][0]
            after_ts = last_ts

            logger.info(f"Fetched page {page+1}: {len(candles)} candles, oldest={last_ts}")

        except Exception as e:
            logger.error(f"Candles fetch exception page {page}: {e}")
            break

    # 转换为标准格式并按时间升序排列
    parsed = []
    for c in all_candles:
        try:
            parsed.append({
                "ts": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "vol": float(c[5]),
            })
        except (IndexError, ValueError):
            continue

    # 去重（分页可能有重叠）并按时间升序
    seen = set()
    unique = []
    for p in parsed:
        if p["ts"] not in seen:
            seen.add(p["ts"])
            unique.append(p)

    unique.sort(key=lambda x: x["ts"])
    logger.info(f"Total unique candles for {symbol}: {len(unique)}")
    return unique


# ============================================================
# 技术指标计算（纯Python，用于回测）
# ============================================================

def calc_rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """计算RSI序列"""
    rsi = [None] * len(closes)
    if len(closes) < period + 1:
        return rsi

    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = 100 - (100 / (1 + rs))

    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i-1]
        gain = max(diff, 0)
        loss = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100 - (100 / (1 + rs))

    return rsi


def calc_ema(closes: List[float], period: int) -> List[Optional[float]]:
    """计算EMA序列"""
    ema = [None] * len(closes)
    if len(closes) < period:
        return ema

    # 初始SMA
    ema[period-1] = sum(closes[:period]) / period
    multiplier = 2 / (period + 1)

    for i in range(period, len(closes)):
        ema[i] = (closes[i] - ema[i-1]) * multiplier + ema[i-1]

    return ema


def calc_macd(closes: List[float], fast=12, slow=26, signal=9) -> Tuple[List, List, List]:
    """计算MACD (DIF, DEA, Histogram)"""
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)

    dif = [None] * len(closes)
    for i in range(len(closes)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif[i] = ema_fast[i] - ema_slow[i]

    # DEA = EMA(DIF, signal)
    dif_values = [d for d in dif if d is not None]
    dea = [None] * len(closes)
    hist = [None] * len(closes)

    if len(dif_values) >= signal:
        start_idx = next(i for i, d in enumerate(dif) if d is not None)
        dea[start_idx + signal - 1] = sum(dif_values[:signal]) / signal
        multiplier = 2 / (signal + 1)

        for i in range(start_idx + signal, len(closes)):
            if dif[i] is not None and dea[i-1] is not None:
                dea[i] = (dif[i] - dea[i-1]) * multiplier + dea[i-1]

        for i in range(len(closes)):
            if dif[i] is not None and dea[i] is not None:
                hist[i] = (dif[i] - dea[i]) * 2

    return dif, dea, hist


def calc_bollinger(closes: List[float], period: int = 20, std_mult: float = 2.0) -> Tuple[List, List, List]:
    """计算布林带 (upper, middle, lower)"""
    upper = [None] * len(closes)
    middle = [None] * len(closes)
    lower = [None] * len(closes)

    for i in range(period - 1, len(closes)):
        window = closes[i-period+1:i+1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance ** 0.5
        middle[i] = mean
        upper[i] = mean + std_mult * std
        lower[i] = mean - std_mult * std

    return upper, middle, lower


def calc_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[Optional[float]]:
    """计算ATR序列"""
    atr = [None] * len(closes)
    if len(closes) < period + 1:
        return atr

    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        trs.append(tr)

    # 初始ATR = SMA(TR, period)
    if len(trs) >= period:
        atr[period] = sum(trs[:period]) / period
        for i in range(period + 1, len(closes)):
            if i - 1 < len(trs):
                atr[i] = (atr[i-1] * (period - 1) + trs[i-1]) / period

    return atr


def calc_supertrend(highs, lows, closes, period=10, multiplier=3.0):
    """计算SuperTrend方向序列: 1=多, -1=空"""
    atr = calc_atr(highs, lows, closes, period)
    direction = [0] * len(closes)
    upper_band = [0.0] * len(closes)
    lower_band = [0.0] * len(closes)

    for i in range(period + 1, len(closes)):
        if atr[i] is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2
        basic_upper = hl2 + multiplier * atr[i]
        basic_lower = hl2 - multiplier * atr[i]

        # 调整band
        if i > 0 and lower_band[i-1] > 0:
            lower_band[i] = max(basic_lower, lower_band[i-1]) if closes[i-1] > lower_band[i-1] else basic_lower
        else:
            lower_band[i] = basic_lower

        if i > 0 and upper_band[i-1] > 0:
            upper_band[i] = min(basic_upper, upper_band[i-1]) if closes[i-1] < upper_band[i-1] else basic_upper
        else:
            upper_band[i] = basic_upper

        # 方向判断
        if i == period + 1:
            direction[i] = 1 if closes[i] > upper_band[i] else -1
        else:
            if direction[i-1] == 1:
                direction[i] = 1 if closes[i] >= lower_band[i] else -1
            else:
                direction[i] = -1 if closes[i] <= upper_band[i] else 1

    return direction


def calc_cmf(highs, lows, closes, vols, period=20):
    """计算CMF（Chaikin Money Flow）"""
    cmf = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        mfv_sum = 0
        vol_sum = 0
        for j in range(i - period + 1, i + 1):
            hl_range = highs[j] - lows[j]
            if hl_range > 0:
                mf_mult = ((closes[j] - lows[j]) - (highs[j] - closes[j])) / hl_range
            else:
                mf_mult = 0
            mfv_sum += mf_mult * vols[j]
            vol_sum += vols[j]
        cmf[i] = mfv_sum / vol_sum if vol_sum > 0 else 0
    return cmf


# ============================================================
# 8因子评分引擎（回测版）
# ============================================================

def score_bar(i: int, closes, highs, lows, vols,
              rsi, ema5, ema20, dif, dea, hist,
              bb_upper, bb_middle, bb_lower,
              atr, supertrend_dir, cmf) -> float:
    """
    对第i根K线计算8因子综合评分
    返回值: 正=做多, 负=做空, 绝对值=强度
    """
    score = 0.0

    # 1. 趋势因子 (权重1.5)
    trend = 0.0
    if supertrend_dir[i] == 1:
        trend += 0.5
    elif supertrend_dir[i] == -1:
        trend -= 0.5
    if ema5[i] and ema20[i]:
        if ema5[i] > ema20[i]:
            trend += 0.5
        else:
            trend -= 0.5
    score += trend * 1.5

    # 2. 动量因子 (权重1.2)
    momentum = 0.0
    if rsi[i] is not None:
        if rsi[i] < 30:
            momentum = 0.8  # 超卖→做多
        elif rsi[i] < 40:
            momentum = 0.4
        elif rsi[i] > 70:
            momentum = -0.8  # 超买→做空
        elif rsi[i] > 60:
            momentum = -0.4
    score += momentum * 1.2

    # 3. MACD因子 (权重1.2)
    macd_score = 0.0
    if hist[i] is not None:
        if hist[i] > 0:
            macd_score += 0.4
            if i > 0 and hist[i-1] is not None and hist[i] > hist[i-1]:
                macd_score += 0.3  # 柱状图增长
        else:
            macd_score -= 0.4
            if i > 0 and hist[i-1] is not None and hist[i] < hist[i-1]:
                macd_score -= 0.3
    if dif[i] is not None and dea[i] is not None:
        if dif[i] > dea[i]:
            macd_score += 0.3
        else:
            macd_score -= 0.3
    score += max(min(macd_score, 1.0), -1.0) * 1.2

    # 4. 布林带因子 (权重0.8)
    bb_score = 0.0
    if bb_upper[i] and bb_lower[i] and bb_middle[i]:
        bb_range = bb_upper[i] - bb_lower[i]
        if bb_range > 0:
            pos = (closes[i] - bb_lower[i]) / bb_range
            if pos < 0.2:
                bb_score = 0.8  # 接近下轨→做多
            elif pos < 0.35:
                bb_score = 0.4
            elif pos > 0.8:
                bb_score = -0.8  # 接近上轨→做空
            elif pos > 0.65:
                bb_score = -0.4
    score += bb_score * 0.8

    # 5. 资金流因子 (权重1.0)
    flow_score = 0.0
    if cmf[i] is not None:
        if cmf[i] > 0.1:
            flow_score = 0.7
        elif cmf[i] > 0.05:
            flow_score = 0.4
        elif cmf[i] < -0.1:
            flow_score = -0.7
        elif cmf[i] < -0.05:
            flow_score = -0.4
    score += flow_score * 1.0

    # 6. 市场结构因子 - 用价格动量代替（回测时无法获取资金费率）
    # 用最近5根K线的方向作为替代
    if i >= 5:
        recent_change = (closes[i] - closes[i-5]) / closes[i-5]
        if recent_change > 0.03:
            score += 0.5  # 强势上涨
        elif recent_change > 0.01:
            score += 0.3
        elif recent_change < -0.03:
            score -= 0.5
        elif recent_change < -0.01:
            score -= 0.3

    # 7. 聪明钱因子 - 回测时无法获取，跳过（权重0）

    # 8. 多时间框架 - 用长EMA方向代替
    if ema20[i] is not None and i >= 1 and ema20[i-1] is not None:
        if ema20[i] > ema20[i-1]:
            score += 0.5  # 大趋势向上
        else:
            score -= 0.5

    return score


# ============================================================
# 回测引擎主类
# ============================================================

class BacktestEngine:
    """完整8因子策略回测引擎"""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.signal_threshold = self.config.get("signal_threshold", 3.5)
        self.sl_atr_mult = self.config.get("sl_atr_mult", 2.0)
        self.tp1_atr_mult = self.config.get("tp1_atr_mult", 2.0)
        self.tp2_atr_mult = self.config.get("tp2_atr_mult", 3.0)
        self.max_hold_bars = self.config.get("max_hold_bars", 30)
        self.position_size = self.config.get("position_size", 0.1)  # 10%仓位
        self.leverage = self.config.get("leverage", 5)
        self._cache: Dict[str, BacktestResult] = {}
        self._load_cache()

    def run(self, symbol: str, total_candles: int = 600) -> Optional[BacktestResult]:
        """运行完整回测"""
        logger.info(f"Running full backtest for {symbol} ({total_candles} candles)...")

        # 1. 拉取K线数据
        candles = fetch_candles(symbol, "4H", total_candles)
        if len(candles) < 50:
            logger.warning(f"Not enough candles for {symbol}: {len(candles)}")
            return self._cache.get(symbol)

        # 2. 提取价格序列
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        vols = [c["vol"] for c in candles]

        # 3. 计算所有指标
        rsi = calc_rsi(closes, 14)
        ema5 = calc_ema(closes, 5)
        ema20 = calc_ema(closes, 20)
        dif, dea, hist = calc_macd(closes)
        bb_upper, bb_middle, bb_lower = calc_bollinger(closes, 20)
        atr = calc_atr(highs, lows, closes, 14)
        supertrend_dir = calc_supertrend(highs, lows, closes)
        cmf = calc_cmf(highs, lows, closes, vols, 20)

        # 4. 遍历K线生成信号并模拟交易
        trades = []
        in_position = False
        position = None  # {"direction", "entry", "sl", "tp1", "tp2", "bar_idx"}

        # 从第30根开始（确保指标都有值）
        start_bar = 30

        for i in range(start_bar, len(candles) - 1):
            # 如果在仓位中，检查是否触发止损/止盈/超时
            if in_position and position:
                exit_price = None
                exit_reason = None

                if position["direction"] == "long":
                    if lows[i] <= position["sl"]:
                        exit_price = position["sl"]
                        exit_reason = "止损"
                    elif highs[i] >= position["tp1"]:
                        exit_price = position["tp1"]
                        exit_reason = "止盈1"
                elif position["direction"] == "short":
                    if highs[i] >= position["sl"]:
                        exit_price = position["sl"]
                        exit_reason = "止损"
                    elif lows[i] <= position["tp1"]:
                        exit_price = position["tp1"]
                        exit_reason = "止盈1"

                # 超时平仓
                bars_held = i - position["bar_idx"]
                if not exit_price and bars_held >= self.max_hold_bars:
                    exit_price = closes[i]
                    exit_reason = "超时"

                if exit_price:
                    # 计算PnL
                    if position["direction"] == "long":
                        pnl_pct = (exit_price - position["entry"]) / position["entry"] * 100
                    else:
                        pnl_pct = (position["entry"] - exit_price) / position["entry"] * 100

                    # 杠杆效果
                    pnl_pct *= self.leverage

                    trades.append({
                        "direction": position["direction"],
                        "entry": position["entry"],
                        "exit": exit_price,
                        "pnl_pct": round(pnl_pct, 2),
                        "bars_held": bars_held,
                        "reason": exit_reason,
                        "ts": candles[i]["ts"],
                    })

                    in_position = False
                    position = None
                continue

            # 不在仓位中，计算信号
            signal_score = score_bar(
                i, closes, highs, lows, vols,
                rsi, ema5, ema20, dif, dea, hist,
                bb_upper, bb_middle, bb_lower,
                atr, supertrend_dir, cmf
            )

            # 信号强度超过阈值才开仓
            if abs(signal_score) >= self.signal_threshold and atr[i] is not None and atr[i] > 0:
                direction = "long" if signal_score > 0 else "short"
                entry_price = closes[i]

                if direction == "long":
                    sl = entry_price - atr[i] * self.sl_atr_mult
                    tp1 = entry_price + atr[i] * self.tp1_atr_mult
                    tp2 = entry_price + atr[i] * self.tp2_atr_mult
                else:
                    sl = entry_price + atr[i] * self.sl_atr_mult
                    tp1 = entry_price - atr[i] * self.tp1_atr_mult
                    tp2 = entry_price - atr[i] * self.tp2_atr_mult

                position = {
                    "direction": direction,
                    "entry": entry_price,
                    "sl": sl,
                    "tp1": tp1,
                    "tp2": tp2,
                    "bar_idx": i,
                    "score": signal_score,
                }
                in_position = True

        # 5. 计算绩效指标
        result = self._calc_performance(symbol, candles, trades)

        # 6. 缓存
        if result:
            self._cache[symbol] = result
            self._save_cache()

        return result

    def _calc_performance(self, symbol: str, candles: List[Dict], trades: List[Dict]) -> Optional[BacktestResult]:
        """计算绩效指标"""
        if not trades:
            return BacktestResult(
                symbol=symbol, timeframe="4H",
                period_days=len(candles) * 4 // 24,
                total_trades=0, win_trades=0, lose_trades=0,
                win_rate=0, profit_factor=0,
                avg_win_pct=0, avg_loss_pct=0,
                total_return_pct=0, max_drawdown_pct=0,
                sharpe_ratio=0, avg_hold_bars=0,
                long_trades=0, short_trades=0,
                long_win_rate=0, short_win_rate=0,
                last_trades=[], timestamp=int(time.time()),
            )

        # 基础统计
        wins = [t for t in trades if t["pnl_pct"] > 0]
        losses = [t for t in trades if t["pnl_pct"] <= 0]
        longs = [t for t in trades if t["direction"] == "long"]
        shorts = [t for t in trades if t["direction"] == "short"]
        long_wins = [t for t in longs if t["pnl_pct"] > 0]
        short_wins = [t for t in shorts if t["pnl_pct"] > 0]

        total_trades = len(trades)
        win_rate = len(wins) / total_trades if total_trades > 0 else 0

        # 盈亏比
        total_profit = sum(t["pnl_pct"] for t in wins) if wins else 0
        total_loss = abs(sum(t["pnl_pct"] for t in losses)) if losses else 0.001
        profit_factor = total_profit / total_loss if total_loss > 0 else 0

        # 平均盈亏
        avg_win = total_profit / len(wins) if wins else 0
        avg_loss = total_loss / len(losses) if losses else 0

        # 总收益（复利）
        equity = 100.0
        equity_curve = [100.0]
        for t in trades:
            # 每笔交易用10%仓位
            trade_return = t["pnl_pct"] * self.position_size
            equity *= (1 + trade_return / 100)
            equity_curve.append(equity)

        total_return_pct = (equity - 100) / 100 * 100  # 百分比

        # 最大回撤
        peak = equity_curve[0]
        max_dd = 0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # Sharpe Ratio（年化，假设4H K线）
        returns = [t["pnl_pct"] * self.position_size for t in trades]
        if len(returns) >= 2:
            avg_ret = sum(returns) / len(returns)
            variance = sum((r - avg_ret) ** 2 for r in returns) / (len(returns) - 1)
            std_ret = variance ** 0.5
            # 年化：4H K线一年约2190根，平均每笔持仓avg_hold_bars根
            avg_hold = sum(t["bars_held"] for t in trades) / len(trades)
            trades_per_year = 2190 / max(avg_hold, 1) if avg_hold > 0 else 100
            sharpe = (avg_ret / std_ret) * (trades_per_year ** 0.5) if std_ret > 0 else 0
        else:
            sharpe = 0
            avg_hold = 0

        avg_hold_bars = sum(t["bars_held"] for t in trades) / len(trades)

        return BacktestResult(
            symbol=symbol,
            timeframe="4H",
            period_days=len(candles) * 4 // 24,
            total_trades=total_trades,
            win_trades=len(wins),
            lose_trades=len(losses),
            win_rate=win_rate,
            profit_factor=profit_factor,
            avg_win_pct=avg_win,
            avg_loss_pct=avg_loss,
            total_return_pct=round(total_return_pct, 2),
            max_drawdown_pct=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 2),
            avg_hold_bars=round(avg_hold_bars, 1),
            long_trades=len(longs),
            short_trades=len(shorts),
            long_win_rate=len(long_wins) / len(longs) if longs else 0,
            short_win_rate=len(short_wins) / len(shorts) if shorts else 0,
            last_trades=trades[-10:],
            timestamp=int(time.time()),
        )

    def _load_cache(self):
        """加载缓存"""
        try:
            if os.path.exists(BACKTEST_CACHE_FILE):
                with open(BACKTEST_CACHE_FILE, 'r') as f:
                    data = json.load(f)
                for key, val in data.items():
                    # 兼容旧字段
                    if "win_trades" not in val:
                        val["win_trades"] = int(val.get("total_trades", 0) * val.get("win_rate", 0))
                        val["lose_trades"] = val.get("total_trades", 0) - val["win_trades"]
                    if "avg_win_pct" not in val:
                        val["avg_win_pct"] = val.get("avg_return_pct", 0)
                        val["avg_loss_pct"] = 0
                    if "avg_hold_bars" not in val:
                        val["avg_hold_bars"] = 0
                    if "long_trades" not in val:
                        val["long_trades"] = 0
                        val["short_trades"] = 0
                        val["long_win_rate"] = 0
                        val["short_win_rate"] = 0
                    if "last_trades" not in val:
                        val["last_trades"] = val.get("last_5_signals", [])
                    # 移除旧字段
                    for old_key in ["avg_return_pct", "last_5_signals"]:
                        val.pop(old_key, None)
                    try:
                        self._cache[key] = BacktestResult(**val)
                    except TypeError:
                        pass
                logger.info(f"Loaded {len(self._cache)} cached backtest results")
        except Exception as e:
            logger.warning(f"Failed to load cache: {e}")

    def _save_cache(self):
        """保存缓存"""
        try:
            data = {k: v.to_dict() for k, v in self._cache.items()}
            with open(BACKTEST_CACHE_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save cache: {e}")


# ============================================================
# 公开API接口（兼容Bot层调用）
# ============================================================

_engine: Optional[BacktestEngine] = None


def get_backtester() -> BacktestEngine:
    """获取全局回测实例"""
    global _engine
    if _engine is None:
        _engine = BacktestEngine()
    return _engine


def run_quick(symbol: str) -> Optional[BacktestResult]:
    """快速回测（300根K线 = 50天）"""
    return get_backtester().run(symbol, total_candles=300)


def run_full(symbol: str) -> Optional[BacktestResult]:
    """完整回测（600根K线 = 100天）"""
    return get_backtester().run(symbol, total_candles=600)


def get_performance(symbol: str) -> Optional[str]:
    """获取绩效摘要文本"""
    bt = get_backtester()
    result = bt._cache.get(symbol)
    if result:
        return result.summary_text
    return None


def get_performance_data(symbol: str) -> Optional[Dict]:
    """获取绩效数据字典"""
    bt = get_backtester()
    result = bt._cache.get(symbol)
    if result:
        return result.to_dict()
    return None


def get_detail(symbol: str) -> Optional[str]:
    """获取详细绩效文本"""
    bt = get_backtester()
    result = bt._cache.get(symbol)
    if result:
        return result.detail_text
    return None


# ============================================================
# 独立运行
# ============================================================

if __name__ == "__main__":
    import sys
    print("=" * 60)
    print("H V3 回测验证层 v3.3 - 完整8因子策略回测")
    print("=" * 60)

    symbols = sys.argv[1:] if len(sys.argv) > 1 else ["BTC", "ETH", "SOL"]

    engine = BacktestEngine()

    for symbol in symbols:
        print(f"\n{'='*40}")
        print(f"  {symbol} 完整回测 (600根4H K线)")
        print(f"{'='*40}")

        result = engine.run(symbol, total_candles=600)

        if result:
            print(f"\n{result.detail_text}")
            print(f"\n最近5笔交易:")
            for t in result.last_trades[-5:]:
                icon = "✅" if t["pnl_pct"] > 0 else "❌"
                print(f"  {icon} {t['direction']} | 入{t['entry']:.1f} → 出{t['exit']:.1f} | "
                      f"{t['pnl_pct']:+.1f}% | {t['bars_held']}根 | {t['reason']}")
        else:
            print("  回测失败")

    print(f"\n{'='*60}")
    print("✓ 回测完成")
