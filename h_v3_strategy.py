"""
H V3 策略引擎 (Strategy Engine)
================================
四层架构第二层：多因子评分策略引擎

设计原则：
1. 只从数据接口层(h_v3_data_api)读取标准格式数据
2. 不自己计算任何指标，所有指标由数据层提供
3. 8+因子多维度评分，权重可调
4. 输出标准信号格式，供回测层和Bot层消费

因子体系（8大因子，聪明钱领先指标提权）：
┌─────────────────────────────────────────────────┐
│ 1. 趋势因子 (SuperTrend + VWAP)        权重 1.2 │
│ 2. 动量因子 (RSI + StochRSI)           权重 1.0 │
│ 3. MACD因子 (DIF/DEA交叉 + 柱状图)     权重 0.8 │
│ 4. 布林带因子 (价格位置 + 带宽)         权重 0.8 │
│ 5. 资金流因子 (CMF + OBV + MFI)        权重 1.0 │
│ 6. 市场结构因子 (资金费率 + OI + 多空比) 权重 1.0 │
│ 7. 聪明钱因子 (三层信号质量)       权重 2.5 │
│ 8. 多时间框架因子 (1H/4H/1D共振)       权重 1.0 │
└─────────────────────────────────────────────────┘
满分: ±9.3 (所有因子加权求和)
"""

import time
import logging
from typing import Dict, Optional, Any, List, Tuple
from dataclasses import dataclass

# 导入数据接口层
try:
    import h_v3_data_api as data_api
except ImportError:
    import sys
    sys.path.insert(0, '/home/ubuntu/h_v3')
    import h_v3_data_api as data_api

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [Strategy] %(levelname)s: %(message)s'
)
logger = logging.getLogger("h_v3_strategy")


# ============================================================
# 信号输出格式
# ============================================================

@dataclass
class Signal:
    """策略引擎输出的标准信号"""
    symbol: str
    direction: str  # "long" / "short" / "neutral"
    strength: float  # -9.0 ~ +9.0
    confidence: int  # 1-5 星级
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    leverage_suggest: int = 5
    # 各因子得分明细
    factor_scores: Dict[str, float] = None
    # 关键数据摘要
    summary: Dict[str, Any] = None
    # 元数据
    timestamp: int = 0
    data_source: str = "okx_cli"

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "strength": self.strength,
            "confidence": self.confidence,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "leverage_suggest": self.leverage_suggest,
            "factor_scores": self.factor_scores,
            "summary": self.summary,
            "timestamp": self.timestamp,
            "data_source": self.data_source,
        }


# ============================================================
# 因子权重配置
# ============================================================

FACTOR_WEIGHTS = {
    "trend": 1.2,       # 趋势因子（滞后指标，降权）
    "momentum": 1.0,    # 动量因子
    "macd": 0.8,        # MACD因子（滞后指标，降权）
    "bollinger": 0.8,   # 布林带因子
    "money_flow": 1.0,  # 资金流因子
    "market_structure": 1.0,  # 市场结构因子
    "smart_money": 2.5,  # 聪明钱因子（领先指标，大幅提权）
    "multi_tf": 1.0,    # 多时间框架因子
}

# 信号阈值
SIGNAL_THRESHOLD_STRONG = 3.5   # 强信号阈值
SIGNAL_THRESHOLD_MEDIUM = 2.0   # 中等信号阈值
SIGNAL_THRESHOLD_WEAK = 1.0     # 弱信号（观望）


# ============================================================
# 因子评分函数
# ============================================================

def score_trend(data: Dict) -> float:
    """
    趋势因子：SuperTrend方向 + 价格vs VWAP
    满分 ±1.0
    """
    score = 0.0
    count = 0

    # SuperTrend方向 (权重60%)
    st_dir = data.get("supertrend_direction")
    if st_dir:
        count += 1
        if st_dir in ("UP", "buy"):
            score += 0.6
        elif st_dir in ("DOWN", "sell"):
            score -= 0.6

    # 价格 vs VWAP (权重40%)
    price = data.get("price")
    vwap = data.get("vwap")
    if price and vwap and vwap > 0:
        count += 1
        deviation = (price - vwap) / vwap
        if deviation > 0.01:
            score += 0.4
        elif deviation < -0.01:
            score -= 0.4
        else:
            # 接近VWAP，微弱信号
            score += deviation * 40  # 线性映射

    return _clamp(score, -1.0, 1.0)


def score_momentum(data: Dict) -> float:
    """
    动量因子：RSI + StochRSI
    满分 ±1.0
    """
    score = 0.0

    # RSI (权重50%)
    rsi = data.get("rsi_14")
    if rsi is not None:
        if rsi >= 70:
            score -= 0.5  # 超买
        elif rsi >= 60:
            score += 0.3  # 偏多
        elif rsi <= 30:
            score += 0.5  # 超卖反弹
        elif rsi <= 40:
            score -= 0.3  # 偏空
        else:
            # 50附近中性，用线性映射
            score += (rsi - 50) / 50 * 0.2

    # StochRSI (权重50%)
    stoch_k = data.get("stoch_rsi_k")
    stoch_d = data.get("stoch_rsi_d")
    if stoch_k is not None and stoch_d is not None:
        if stoch_k > stoch_d and stoch_k < 80:
            score += 0.5  # 金叉且未超买
        elif stoch_k < stoch_d and stoch_k > 20:
            score -= 0.5  # 死叉且未超卖
        elif stoch_k >= 80:
            score -= 0.3  # 超买区
        elif stoch_k <= 20:
            score += 0.3  # 超卖区

    return _clamp(score, -1.0, 1.0)


def score_macd(data: Dict) -> float:
    """
    MACD因子：DIF/DEA交叉 + 柱状图方向
    满分 ±1.0
    """
    score = 0.0

    dif = data.get("macd_dif")
    dea = data.get("macd_dea")
    hist = data.get("macd_hist")

    if dif is not None and dea is not None:
        # DIF vs DEA (权重60%)
        if dif > dea:
            score += 0.6
        elif dif < dea:
            score -= 0.6

        # 柱状图方向和力度 (权重40%)
        if hist is not None:
            if hist > 0:
                score += min(0.4, abs(hist) / 500 * 0.4)
            else:
                score -= min(0.4, abs(hist) / 500 * 0.4)

    return _clamp(score, -1.0, 1.0)


def score_bollinger(data: Dict) -> float:
    """
    布林带因子：价格在布林带中的位置
    满分 ±1.0
    """
    score = 0.0

    price = data.get("price")
    upper = data.get("bb_upper")
    lower = data.get("bb_lower")
    middle = data.get("bb_middle")

    if price and upper and lower and middle and (upper - lower) > 0:
        # 价格位置 (0=下轨, 0.5=中轨, 1=上轨)
        position = (price - lower) / (upper - lower)

        if position >= 0.9:
            score -= 0.8  # 接近上轨，可能回调
        elif position >= 0.7:
            score += 0.3  # 强势但注意
        elif position <= 0.1:
            score += 0.8  # 接近下轨，可能反弹
        elif position <= 0.3:
            score -= 0.3  # 弱势但注意
        else:
            # 中间区域，根据偏离中轨方向
            score += (position - 0.5) * 0.4

        # 带宽因子：带宽收窄可能突破
        bandwidth = (upper - lower) / middle if middle > 0 else 0
        if bandwidth < 0.02:
            # 极度收窄，方向不确定但波动即将放大
            score *= 0.5  # 减弱信号

    return _clamp(score, -1.0, 1.0)


def score_money_flow(data: Dict) -> float:
    """
    资金流因子：CMF + OBV趋势 + MFI
    满分 ±1.0
    """
    score = 0.0
    count = 0

    # CMF (权重35%)
    cmf = data.get("cmf")
    if cmf is not None:
        count += 1
        if cmf > 0.1:
            score += 0.35
        elif cmf > 0:
            score += 0.15
        elif cmf < -0.1:
            score -= 0.35
        elif cmf < 0:
            score -= 0.15

    # OBV vs MA (权重30%)
    obv = data.get("obv")
    obv_ma = data.get("obv_ma")
    if obv is not None and obv_ma is not None and obv_ma != 0:
        count += 1
        if obv > obv_ma:
            score += 0.30
        else:
            score -= 0.30

    # MFI (权重35%)
    mfi = data.get("mfi")
    if mfi is not None:
        count += 1
        if mfi >= 80:
            score -= 0.35  # 资金流出超买
        elif mfi >= 60:
            score += 0.20  # 资金流入
        elif mfi <= 20:
            score += 0.35  # 超卖，资金可能流入
        elif mfi <= 40:
            score -= 0.20  # 资金流出

    return _clamp(score, -1.0, 1.0)


def score_market_structure(data: Dict) -> float:
    """
    市场结构因子：资金费率 + 持仓量变化 + 多空比
    满分 ±1.0
    """
    score = 0.0

    # 资金费率 (权重40%) - 负费率利多，正费率利空（逆向）
    fr = data.get("funding_rate")
    if fr is not None:
        if fr < -0.001:
            score += 0.4  # 强负费率，空头付费，利多
        elif fr < 0:
            score += 0.2
        elif fr > 0.001:
            score -= 0.4  # 强正费率，多头付费，利空
        elif fr > 0:
            score -= 0.2

    # 持仓量变化 (权重30%)
    oi_change = data.get("oi_change_pct")
    if oi_change is not None:
        # OI增加配合价格上涨=多头强势，OI增加配合价格下跌=空头强势
        # 这里简化：OI增加视为趋势延续信号
        price = data.get("price")
        price_open = data.get("price_open")
        if price and price_open and price_open > 0:
            price_change = (price - price_open) / price_open
            if oi_change > 0 and price_change > 0:
                score += 0.3  # OI增+价涨=多头加仓
            elif oi_change > 0 and price_change < 0:
                score -= 0.3  # OI增+价跌=空头加仓
            elif oi_change < -1:
                # OI大幅减少=平仓，趋势可能反转
                score += 0.1 if price_change < 0 else -0.1

    # 多空比 (权重30%) - 逆向指标
    long_ratio = data.get("long_ratio")
    short_ratio = data.get("short_ratio")
    if long_ratio is not None and short_ratio is not None:
        # 散户多空比是逆向指标：散户做多过多→利空
        if long_ratio > 0.6:
            score -= 0.3  # 散户过度做多，逆向做空
        elif short_ratio > 0.6:
            score += 0.3  # 散户过度做空，逆向做多
        # 均衡时不给分

    return _clamp(score, -1.0, 1.0)


def score_smart_money(data: Dict) -> float:
    """
    聪明钱因子：三层信号质量评估
    满分 ±1.0
    
    评分维度：
    1. 全量方向 + 强度 (基础分)
    2. 三层一致性 (加分/减分)
    3. 胜率加权 (高胜率方向加分)
    4. 趋势动量 (持续加强加分)
    5. 大资金反向警告 (强制降权)
    """
    score = 0.0

    direction = data.get("smart_money_direction")
    long_pct = data.get("smart_money_long_pct")
    quality_consensus = data.get("smart_money_quality_consensus")
    quality_score = data.get("smart_money_quality_score")
    elite_long = data.get("smart_money_elite_long_pct")
    whale_long = data.get("smart_money_whale_long_pct")
    avg_long_wr = data.get("smart_money_avg_long_wr")
    avg_short_wr = data.get("smart_money_avg_short_wr")
    vs24h = data.get("smart_money_vs24h")
    vs7d = data.get("smart_money_vs7d")

    if direction and long_pct is not None:
        # === 第一层：基础方向分 ===
        if direction == "long":
            if long_pct >= 0.8:
                score = 0.7
            elif long_pct >= 0.7:
                score = 0.55
            elif long_pct >= 0.6:
                score = 0.35
            else:
                score = 0.2
        elif direction == "short":
            short_pct = 1.0 - long_pct
            if short_pct >= 0.8:
                score = -0.7
            elif short_pct >= 0.7:
                score = -0.55
            elif short_pct >= 0.6:
                score = -0.35
            else:
                score = -0.2

        # === 第二层：三层一致性修正 ===
        if quality_consensus == "strong":
            score *= 1.4  # 三层一致，放大信号
        elif quality_consensus == "divergent":
            score *= 0.4  # 大资金反向，强制压缩
        elif quality_consensus == "weak":
            score *= 0.8  # 部分一致，略微降权

        # === 第三层：胜率加权 ===
        if score > 0 and avg_long_wr and avg_long_wr > 0.8:
            score += 0.1  # 多方高胜率加分
        elif score < 0 and avg_short_wr and avg_short_wr > 0.8:
            score -= 0.1  # 空方高胜率加分

        # === 第四层：趋势动量 ===
        if vs24h is not None and vs7d is not None:
            if score > 0 and vs24h > 0 and vs7d > 0:
                score += 0.1  # 多头持续加强
            elif score < 0 and vs24h < 0 and vs7d < 0:
                score -= 0.1  # 空头持续加强
            elif score > 0 and vs24h < -0.1:
                score -= 0.05  # 多头但趋势转弱
            elif score < 0 and vs24h > 0.1:
                score += 0.05  # 空头但趋势转弱

        # === 第五层：大资金反向警告 ===
        if whale_long is not None and direction == "long" and whale_long < 0.35:
            score *= 0.5  # 全量看多但大资金看空，危险！
        elif whale_long is not None and direction == "short" and whale_long > 0.65:
            score *= 0.5  # 全量看空但大资金看多，危险！

    return _clamp(score, -1.0, 1.0)


def score_multi_timeframe(data: Dict) -> float:
    """
    多时间框架因子：1H/4H/1D方向共振
    满分 ±1.0
    """
    score = 0.0

    tf_1h = data.get("tf_1h", {}).get("direction", "neutral")
    tf_1d = data.get("tf_1d", {}).get("direction", "neutral")

    # 4H方向由主因子决定，这里看1H和1D是否共振
    directions = {"long": 1, "short": -1, "neutral": 0}

    d_1h = directions.get(tf_1h, 0)
    d_1d = directions.get(tf_1d, 0)

    # 1H权重40%, 1D权重60%
    score = d_1h * 0.4 + d_1d * 0.6

    # 如果1H和1D同向，加强信号
    if d_1h != 0 and d_1h == d_1d:
        score = d_1h * 1.0  # 满分

    return _clamp(score, -1.0, 1.0)


# ============================================================
# 策略引擎主类
# ============================================================

class StrategyEngine:
    """
    多因子评分策略引擎
    
    使用方式：
        engine = StrategyEngine()
        signal = engine.analyze("BTC")
    """

    def __init__(self, weights: Dict[str, float] = None):
        """
        Args:
            weights: 自定义因子权重，默认使用 FACTOR_WEIGHTS
        """
        self.weights = weights or FACTOR_WEIGHTS
        self.max_score = sum(self.weights.values())

    def analyze(self, symbol: str, data: Dict = None) -> Signal:
        """
        分析一个币种，输出交易信号
        
        Args:
            symbol: 币种名称
            data: 标准格式数据（可选，不传则从数据接口层获取）
            
        Returns:
            Signal对象
        """
        # 从数据接口层获取数据
        if data is None:
            data = data_api.get_data(symbol)
        if data is None:
            logger.warning(f"No data for {symbol}")
            return Signal(
                symbol=symbol, direction="neutral",
                strength=0, confidence=0,
                timestamp=int(time.time())
            )

        # 计算各因子得分
        factor_scores = {
            "trend": score_trend(data) * self.weights["trend"],
            "momentum": score_momentum(data) * self.weights["momentum"],
            "macd": score_macd(data) * self.weights["macd"],
            "bollinger": score_bollinger(data) * self.weights["bollinger"],
            "money_flow": score_money_flow(data) * self.weights["money_flow"],
            "market_structure": score_market_structure(data) * self.weights["market_structure"],
            "smart_money": score_smart_money(data) * self.weights["smart_money"],
            "multi_tf": score_multi_timeframe(data) * self.weights["multi_tf"],
        }

        # 总分
        total_score = sum(factor_scores.values())

        # 方向判断
        if total_score >= SIGNAL_THRESHOLD_MEDIUM:
            direction = "long"
        elif total_score <= -SIGNAL_THRESHOLD_MEDIUM:
            direction = "short"
        else:
            direction = "neutral"

        # 信心等级 (1-5星)
        abs_score = abs(total_score)
        if abs_score >= SIGNAL_THRESHOLD_STRONG * 1.5:
            confidence = 5
        elif abs_score >= SIGNAL_THRESHOLD_STRONG:
            confidence = 4
        elif abs_score >= SIGNAL_THRESHOLD_MEDIUM:
            confidence = 3
        elif abs_score >= SIGNAL_THRESHOLD_WEAK:
            confidence = 2
        else:
            confidence = 1

        # === 聪明钱方向矛盾警告 ===
        # 当聪明钱三层强共识与技术面方向相反时，降低置信度
        sm_direction = data.get("smart_money_direction")
        sm_quality = data.get("smart_money_quality_consensus")
        sm_conflict = False
        if sm_quality == "strong" and sm_direction and direction != "neutral":
            if (direction == "long" and sm_direction == "short") or \
               (direction == "short" and sm_direction == "long"):
                confidence = max(1, confidence - 1)  # 降1星
                sm_conflict = True
                logger.info(f"{symbol}: ⚠️ 聪明钱强共识{sm_direction}与技术面{direction}矛盾，置信度降至{confidence}星")

        # 计算止损止盈（基于ATR）
        entry_price = data.get("price")
        stop_loss = None
        take_profit_1 = None
        take_profit_2 = None
        leverage_suggest = 5

        atr = data.get("atr_14")
        # 聪明钱矛盾时收紧止损
        sl_multiplier = 1.5 if sm_conflict else 2.0
        if entry_price and atr and atr > 0:
            if direction == "long":
                stop_loss = round(entry_price - atr * sl_multiplier, 1)
                take_profit_1 = round(entry_price + atr * 2.0, 1)
                take_profit_2 = round(entry_price + atr * 3.5, 1)
            elif direction == "short":
                stop_loss = round(entry_price + atr * sl_multiplier, 1)
                take_profit_1 = round(entry_price - atr * 2.0, 1)
                take_profit_2 = round(entry_price - atr * 3.5, 1)

            # 杠杆建议（基于ATR波动率）
            volatility = atr / entry_price
            if volatility > 0.03:
                leverage_suggest = 3
            elif volatility > 0.02:
                leverage_suggest = 5
            elif volatility > 0.01:
                leverage_suggest = 8
            else:
                leverage_suggest = 10

        # 构建摘要
        summary = {
            "price": entry_price,
            "rsi": data.get("rsi_14"),
            "macd_hist": data.get("macd_hist"),
            "supertrend": data.get("supertrend_direction"),
            "ema_5": data.get("ema_5"),
            "ema_20": data.get("ema_20"),
            "funding_rate": data.get("funding_rate"),
            "long_ratio": data.get("long_ratio"),
            "oi_change": data.get("oi_change_pct"),
            "tf_1h": data.get("tf_1h", {}).get("direction"),
            "tf_1d": data.get("tf_1d", {}).get("direction"),
            # 聪明钱三层质量
            "smart_money": data.get("smart_money_consensus"),
            "sm_quality": data.get("smart_money_quality_consensus"),  # strong/weak/divergent
            "sm_quality_score": data.get("smart_money_quality_score"),
            "sm_all_long": data.get("smart_money_weighted_long"),
            "sm_elite_long": data.get("smart_money_elite_long_pct"),
            "sm_whale_long": data.get("smart_money_whale_long_pct"),
            "sm_long_wr": data.get("smart_money_avg_long_wr"),
            "sm_short_wr": data.get("smart_money_avg_short_wr"),
            "sm_vs24h": data.get("smart_money_vs24h"),
            "sm_vs7d": data.get("smart_money_vs7d"),
            "sm_net_notional": data.get("smart_money_net_notional"),
            "sm_long_entry": data.get("smart_money_long_entry"),
            "sm_short_entry": data.get("smart_money_short_entry"),
            "sm_traders": data.get("smart_money_traders_count"),
            "sm_conflict": sm_conflict,  # 聪明钱与技术面方向矛盾警告
        }

        return Signal(
            symbol=symbol,
            direction=direction,
            strength=round(total_score, 2),
            confidence=confidence,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            leverage_suggest=leverage_suggest,
            factor_scores=factor_scores,
            summary=summary,
            timestamp=int(time.time()),
            data_source=data.get("data_source", "okx_cli"),
        )

    def analyze_all(self, symbols: List[str] = None) -> List[Signal]:
        """分析所有监控币种"""
        if symbols is None:
            symbols = data_api.SYMBOLS
        signals = []
        for symbol in symbols:
            sig = self.analyze(symbol)
            signals.append(sig)
        return signals

    def get_top_signals(self, min_confidence: int = 3) -> List[Signal]:
        """获取高置信度信号（用于推送）"""
        all_signals = self.analyze_all()
        return [s for s in all_signals if s.confidence >= min_confidence]


# ============================================================
# 工具函数
# ============================================================

def _clamp(value: float, min_val: float, max_val: float) -> float:
    """限制值在范围内"""
    return max(min_val, min(max_val, value))


# ============================================================
# 全局引擎实例
# ============================================================

_engine: Optional[StrategyEngine] = None


def get_engine() -> StrategyEngine:
    """获取全局策略引擎实例"""
    global _engine
    if _engine is None:
        _engine = StrategyEngine()
    return _engine


def analyze(symbol: str, data: Dict = None) -> Signal:
    """快捷分析函数"""
    return get_engine().analyze(symbol, data)


def analyze_all() -> List[Signal]:
    """快捷分析所有币种"""
    return get_engine().analyze_all()


# ============================================================
# 独立运行测试
# ============================================================

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("H V3 策略引擎 - 独立测试")
    print("=" * 60)

    # 模拟数据测试（不依赖数据接口层运行）
    mock_data = {
        "symbol": "BTC",
        "price": 81383.6,
        "price_open": 80875.3,
        "rsi_14": 66.69,
        "macd_dif": 926.7,
        "macd_dea": 823.9,
        "macd_hist": 205.6,
        "bb_upper": 82100.1,
        "bb_middle": 79807.4,
        "bb_lower": 77514.7,
        "atr_14": 882.9,
        "supertrend_value": 78627.4,
        "supertrend_direction": "buy",
        "vwap": 80115.6,
        "cmf": 0.10,
        "obv": 14021253,
        "obv_ma": 9390280,
        "kdj_k": 83.26,
        "kdj_d": 82.53,
        "kdj_j": 84.71,
        "stoch_rsi": 54.05,
        "stoch_rsi_k": 57.93,
        "stoch_rsi_d": 66.51,
        "mfi": 76.45,
        "funding_rate": -0.0000072,
        "oi_usd": 2877470562.87,
        "oi_change_pct": -0.22,
        "long_ratio": 0.46,
        "short_ratio": 0.54,
        "smart_money_long_pct": 0.67,
        "smart_money_direction": "long",
        "smart_money_consensus": "中共识做多",
        "tf_1h": {"direction": "long"},
        "tf_1d": {"direction": "neutral"},
        "data_source": "okx_cli",
    }

    engine = StrategyEngine()
    signal = engine.analyze("BTC", mock_data)

    print(f"\n--- BTC 信号分析 ---")
    print(f"方向: {signal.direction}")
    print(f"强度: {signal.strength}")
    print(f"信心: {'⭐️' * signal.confidence} ({signal.confidence}/5)")
    print(f"入场: {signal.entry_price}")
    print(f"止损: {signal.stop_loss}")
    print(f"止盈1: {signal.take_profit_1}")
    print(f"止盈2: {signal.take_profit_2}")
    print(f"杠杆建议: {signal.leverage_suggest}x")
    print(f"\n--- 因子得分明细 ---")
    for name, score in signal.factor_scores.items():
        weight = FACTOR_WEIGHTS[name]
        raw = score / weight if weight > 0 else 0
        bar = "█" * int(abs(raw) * 10) if raw != 0 else "·"
        direction = "+" if score > 0 else "-" if score < 0 else " "
        print(f"  {name:20s}: {direction}{abs(score):.3f} (raw={raw:.2f}, w={weight})")
    print(f"\n  总分: {signal.strength} / ±{engine.max_score:.1f}")
    print(f"\n✓ 测试完成")
