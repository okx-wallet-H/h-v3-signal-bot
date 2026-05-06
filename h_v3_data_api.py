"""
H V3 数据接口层 (Data API Layer)
================================
四层架构第一层：可插拔数据源适配器

设计原则：
1. 引擎只认标准格式数据，不关心数据从哪来
2. 数据接口层是适配器，今天OKX，明天可以换任何数据源
3. 后台持续拉数据存缓存，用户请求时秒回
4. 任何单个命令失败不阻塞，使用上次有效数据（容错降级）

数据源：
- okx-cex-market (CLI): 行情数据（价格、K线、盘口、资金费率、技术指标）- 无需鉴权
- OKX V5 REST API: 聪明钱（signal/overview）- 需鉴权（绕过CLI bug）
"""

import json
import subprocess
import time
import hmac
import hashlib
import base64
import threading
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional, Any, List
from datetime import datetime, timezone
from copy import deepcopy

# ============================================================
# 配置
# ============================================================

SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "OKB"]
TIMEFRAMES = ["1H", "4H", "1Dutc"]
DEFAULT_TIMEFRAME = "4H"
CACHE_TTL = 300  # 缓存有效期5分钟
REFRESH_INTERVAL = 300  # 后台刷新间隔5分钟
CLI_TIMEOUT = 10  # 单个CLI命令超时10秒
MAX_WORKERS = 12  # 并行线程数

# OKX V5 API 配置（用于聪明钱，绕过CLI bug）
OKX_API_KEY = 'YOUR_OKX_API_KEY'
OKX_SECRET_KEY = 'YOUR_OKX_SECRET_KEY'
OKX_PASSPHRASE = 'YOUR_OKX_PASSPHRASE'
OKX_BASE_URL = 'https://www.okx.com'

# 技术指标列表（OKX CLI支持的）
INDICATORS = [
    "rsi", "macd", "bb", "atr", "supertrend",
    "vwap", "cmf", "obv", "kdj", "stoch-rsi", "mfi",
    "top-long-short", "ema"
]

# EMA 需要特殊参数（注意：supertrend不能传params，传了返回空）
INDICATOR_PARAMS = {
    "ema": "5,20",
    "rsi": "14",
    "macd": "12,26,9",
    "bb": "20,2",
    "kdj": "9,3,3",
}

# 某些指标在SWAP上返回空，需要用现货instId
INDICATORS_USE_SPOT = {"supertrend", "ema"}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [DataAPI] %(levelname)s: %(message)s'
)
logger = logging.getLogger("h_v3_data_api")


# ============================================================
# 标准数据格式定义
# ============================================================

def empty_standard_data(symbol: str) -> Dict[str, Any]:
    """返回空的标准格式数据结构"""
    return {
        "symbol": symbol.upper(),
        "timestamp": 0,

        # 价格
        "price": None,
        "price_open": None,
        "price_high": None,
        "price_low": None,
        "volume_24h": None,

        # 技术指标
        "rsi_14": None,
        "macd_dif": None,
        "macd_dea": None,
        "macd_hist": None,
        "bb_upper": None,
        "bb_middle": None,
        "bb_lower": None,
        "atr_14": None,
        "supertrend_value": None,
        "supertrend_direction": None,  # "buy" / "sell"
        "vwap": None,
        "cmf": None,
        "obv": None,
        "obv_ma": None,
        "kdj_k": None,
        "kdj_d": None,
        "kdj_j": None,
        "stoch_rsi": None,
        "stoch_rsi_k": None,
        "stoch_rsi_d": None,
        "mfi": None,
        "ema_5": None,
        "ema_20": None,

        # 市场微观结构
        "funding_rate": None,
        "funding_rate_next": None,
        "oi_usd": None,
        "oi_change_pct": None,
        "long_ratio": None,
        "short_ratio": None,
        "long_short_ratio": None,

        # 聪明钱（来自OKX V5 REST API）
        "smart_money_long_pct": None,
        "smart_money_short_pct": None,
        "smart_money_weighted_long": None,
        "smart_money_weighted_short": None,
        "smart_money_direction": None,  # "long" / "short" / "neutral"
        "smart_money_consensus": None,  # 中文描述
        "smart_money_long_entry": None,  # 多方平均入场价
        "smart_money_short_entry": None,  # 空方平均入场价
        "smart_money_traders_count": None,  # 持仓交易员数
        "smart_money_vs1h": None,
        "smart_money_vs24h": None,
        "smart_money_vs7d": None,
        "smart_money_net_notional": None,  # 净名义价值(正=多头主导)
        "smart_money_total_notional_vs24h": None,  # 24h总资金变化率
        # 三层信号质量
        "smart_money_elite_long_pct": None,  # 高质量交易员(WR>=80%+PnL TOP20%)多方占比
        "smart_money_whale_long_pct": None,  # 大资金(AUM TOP20%)多方占比
        "smart_money_avg_long_wr": None,  # 多方平均胜率
        "smart_money_avg_short_wr": None,  # 空方平均胜率
        "smart_money_quality_consensus": None,  # 三层一致性: strong/weak/divergent
        "smart_money_quality_score": None,  # 信号质量评分(0-1)

        # 多时间框架方向（由多TF指标综合判断）
        "tf_1h": {},
        "tf_4h": {},
        "tf_1d": {},

        # 元数据
        "data_source": "okx_cli+v5api",
        "data_version": "v3.2",
        "fetch_time_ms": 0,
        "errors": [],
    }


# ============================================================
# OKX V5 REST API 客户端（用于聪明钱）
# ============================================================

class OKXRestClient:
    """直接调用OKX V5 REST API，绕过CLI的smartmoney bug"""

    @staticmethod
    def _sign(timestamp: str, method: str, request_path: str, body: str = '') -> str:
        message = timestamp + method + request_path + body
        mac = hmac.new(OKX_SECRET_KEY.encode(), message.encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    @staticmethod
    def _get_headers(method: str, request_path: str, body: str = '') -> Dict:
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        sign = OKXRestClient._sign(timestamp, method, request_path, body)
        return {
            'OK-ACCESS-KEY': OKX_API_KEY,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': OKX_PASSPHRASE,
            'Content-Type': 'application/json'
        }

    @staticmethod
    def api_get(path: str, timeout: int = 10) -> Optional[Dict]:
        """发送GET请求到OKX V5 API"""
        try:
            headers = OKXRestClient._get_headers('GET', path)
            resp = requests.get(OKX_BASE_URL + path, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('code') == '0':
                    return data.get('data')
                else:
                    logger.warning(f"OKX API error [{path[:50]}]: code={data.get('code')} msg={data.get('msg')}")
                    return None
            else:
                logger.warning(f"OKX API HTTP {resp.status_code} [{path[:50]}]")
                return None
        except requests.Timeout:
            logger.warning(f"OKX API timeout [{path[:50]}]")
            return None
        except Exception as e:
            logger.error(f"OKX API exception [{path[:50]}]: {e}")
            return None

    @staticmethod
    def get_smart_signal(inst_ccy: str, **filters) -> Optional[Dict]:
        """
        获取单币种聪明钱共识信号
        API: /api/v5/journal/smartmoney/signal?instCcy=BTC
        
        可选筛选参数:
            winRatio: WR_ANY, WR_GE_50, WR_GE_80
            pnl: PNL_ANY, PNL_TOP50, PNL_TOP20, PNL_TOP5
            asset: AUM_ANY, AUM_TOP50, AUM_TOP20, AUM_TOP5
            maxRetreat: MR_ANY, MR_LE_20, MR_LE_50
            lmtNum: 1-500 (候选池大小)
        
        返回: longRatio, weightedLongRatio, avgLongWinRate, avgShortWinRate,
              vs1h, vs24h, vs7d, netNotionalUsdt, smartMoneyLongAvgEntry 等
        """
        params = f'instCcy={inst_ccy}'
        for k, v in filters.items():
            params += f'&{k}={v}'
        path = f'/api/v5/journal/smartmoney/signal?{params}'
        data = OKXRestClient.api_get(path)
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
        return data if isinstance(data, dict) else None

    @staticmethod
    def get_smart_signal_elite(inst_ccy: str) -> Optional[Dict]:
        """获取高质量交易员信号（胜率>=80% + PnL前20%）"""
        return OKXRestClient.get_smart_signal(
            inst_ccy, winRatio='WR_GE_80', pnl='PNL_TOP20', lmtNum='10'
        )

    @staticmethod
    def get_smart_signal_whale(inst_ccy: str) -> Optional[Dict]:
        """获取大资金交易员信号（AUM前20%）"""
        return OKXRestClient.get_smart_signal(
            inst_ccy, asset='AUM_TOP20', lmtNum='10'
        )

    @staticmethod
    def get_smart_overview(top_n: int = 20) -> Optional[List]:
        """
        获取多币种聪明钱总览
        API: /api/v5/journal/smartmoney/overview?topInstruments=20
        """
        path = f'/api/v5/journal/smartmoney/overview?topInstruments={top_n}'
        data = OKXRestClient.api_get(path)
        if data and isinstance(data, list):
            return data
        return None


# ============================================================
# OKX CLI 命令执行器（用于技术指标和行情）
# ============================================================

class OKXCliExecutor:
    """封装OKX CLI命令执行，统一错误处理"""

    @staticmethod
    def run(cmd: str, timeout: int = CLI_TIMEOUT) -> Optional[Any]:
        """执行OKX CLI命令，返回解析后的JSON或None"""
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=timeout
            )
            if result.returncode != 0:
                logger.warning(f"CLI error [{cmd[:60]}]: {result.stderr[:100]}")
                return None
            output = result.stdout.strip()
            if not output:
                return None
            return json.loads(output)
        except subprocess.TimeoutExpired:
            logger.warning(f"CLI timeout [{cmd[:60]}]")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse error [{cmd[:60]}]: {e}")
            return None
        except Exception as e:
            logger.error(f"CLI exception [{cmd[:60]}]: {e}")
            return None

    @staticmethod
    def get_indicator(inst_id: str, indicator: str, bar: str = "4H") -> Optional[Dict]:
        """获取单个技术指标"""
        # 某些指标需要用现货instId（SWAP上返回空）
        actual_inst_id = inst_id
        if indicator in INDICATORS_USE_SPOT and inst_id.endswith("-SWAP"):
            actual_inst_id = inst_id.replace("-SWAP", "")
        # 构建命令，包含params参数
        params_str = INDICATOR_PARAMS.get(indicator, "")
        params_arg = f" --params {params_str}" if params_str else ""
        cmd = f"okx market indicator {indicator} {actual_inst_id} --bar {bar}{params_arg} --json"
        data = OKXCliExecutor.run(cmd)
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
        try:
            indicators = data[0]["data"][0]["timeframes"][bar]["indicators"]
            # 指标名称大写
            key = indicator.upper().replace("-", "")
            # 特殊映射
            key_map = {
                "STOCHRSI": "STOCHRSI",
                "TOPLONGSHORT": "TOPLONGSHORT",
                "BB": "BB",
            }
            key = key_map.get(key, key)
            if key in indicators and len(indicators[key]) > 0:
                return indicators[key][0].get("values", {})
            # 尝试小写
            for k in indicators:
                if k.upper() == key:
                    if len(indicators[k]) > 0:
                        return indicators[k][0].get("values", {})
            return None
        except (KeyError, IndexError, TypeError):
            return None

    @staticmethod
    def get_funding_rate(inst_id: str) -> Optional[Dict]:
        """获取资金费率"""
        cmd = f"okx market funding-rate {inst_id} --json"
        data = OKXCliExecutor.run(cmd)
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
        return data[0] if isinstance(data[0], dict) else None

    @staticmethod
    def get_oi_history(inst_id: str) -> Optional[Dict]:
        """获取持仓量历史（最新一条）"""
        cmd = f"okx market oi-history {inst_id} --json"
        data = OKXCliExecutor.run(cmd)
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
        try:
            rows = data[0].get("rows", [])
            if rows and len(rows) > 0:
                return rows[0]
            return None
        except (KeyError, IndexError, TypeError):
            return None

    @staticmethod
    def get_candles(inst_id: str, bar: str = "4H", limit: int = 1) -> Optional[List]:
        """获取K线数据"""
        cmd = f"okx market candles {inst_id} --bar {bar} --json"
        data = OKXCliExecutor.run(cmd)
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
        return data[:limit]


# ============================================================
# 数据解析器：将OKX CLI原始数据映射到标准格式
# ============================================================

class DataParser:
    """将OKX CLI返回的原始JSON映射到标准数据格式"""

    @staticmethod
    def parse_rsi(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        # 可能返回 {"14": "65.7"} 或 {"rsi": "65.7"}
        val = raw.get("14") or raw.get("rsi")
        return {"rsi_14": _to_float(val)}

    @staticmethod
    def parse_macd(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        return {
            "macd_dif": _to_float(raw.get("dif")),
            "macd_dea": _to_float(raw.get("dea")),
            "macd_hist": _to_float(raw.get("macd")),
        }

    @staticmethod
    def parse_bb(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        return {
            "bb_upper": _to_float(raw.get("upper")),
            "bb_middle": _to_float(raw.get("middle")),
            "bb_lower": _to_float(raw.get("lower")),
        }

    @staticmethod
    def parse_atr(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        # 可能返回 {"14": "1234"} 或 {"atr": "1234"}
        val = raw.get("14") or raw.get("atr")
        return {"atr_14": _to_float(val)}

    @staticmethod
    def parse_supertrend(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        # API实际返回: direction="-1"/"1", trend="UP"/"DOWN"
        # direction: -1=上涨趋势(buy), 1=下跌趋势(sell)
        # trend: "UP"=买入信号, "DOWN"=卖出信号
        trend = raw.get("trend", "").upper()
        dir_val = raw.get("direction", "")
        
        if trend == "UP" or str(dir_val) == "-1":
            direction = "buy"
        elif trend == "DOWN" or str(dir_val) == "1":
            direction = "sell"
        else:
            direction = None
        
        return {
            "supertrend_value": _to_float(raw.get("superTrend") or raw.get("supertrend") or raw.get("lowerBand")),
            "supertrend_direction": direction,
        }

    @staticmethod
    def parse_vwap(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        return {"vwap": _to_float(raw.get("vwap"))}

    @staticmethod
    def parse_cmf(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        return {"cmf": _to_float(raw.get("cmf"))}

    @staticmethod
    def parse_obv(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        return {
            "obv": _to_float(raw.get("value") or raw.get("obv")),
            "obv_ma": _to_float(raw.get("maObv") or raw.get("ma")),
        }

    @staticmethod
    def parse_kdj(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        return {
            "kdj_k": _to_float(raw.get("k")),
            "kdj_d": _to_float(raw.get("d")),
            "kdj_j": _to_float(raw.get("j")),
        }

    @staticmethod
    def parse_stoch_rsi(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        return {
            "stoch_rsi": _to_float(raw.get("stochRsi") or raw.get("stochrsi")),
            "stoch_rsi_k": _to_float(raw.get("k")),
            "stoch_rsi_d": _to_float(raw.get("d")),
        }

    @staticmethod
    def parse_mfi(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        return {"mfi": _to_float(raw.get("mfi"))}

    @staticmethod
    def parse_ema(raw: Optional[Dict]) -> Dict:
        """解析EMA指标 - 返回 {"5": "81144.2", "20": "80033.5"}"""
        if not raw:
            return {}
        return {
            "ema_5": _to_float(raw.get("5") or raw.get("ema5")),
            "ema_20": _to_float(raw.get("20") or raw.get("ema20")),
        }

    @staticmethod
    def parse_top_long_short(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        return {
            "long_ratio": _to_float(raw.get("longRatio")),
            "short_ratio": _to_float(raw.get("shortRatio")),
            "long_short_ratio": _to_float(raw.get("longShortRatio")),
        }

    @staticmethod
    def parse_funding_rate(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        return {
            "funding_rate": _to_float(raw.get("fundingRate")),
            "funding_rate_next": _to_float(raw.get("nextFundingRate")),
        }

    @staticmethod
    def parse_oi(raw: Optional[Dict]) -> Dict:
        if not raw:
            return {}
        return {
            "oi_usd": _to_float(raw.get("oiUsd")),
            "oi_change_pct": _to_float(raw.get("oiDeltaPct")),
        }

    @staticmethod
    def parse_candle(raw: Optional[List]) -> Dict:
        """K线格式: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]"""
        if not raw or len(raw) < 7:
            return {}
        return {
            "price": _to_float(raw[4]),  # close
            "price_open": _to_float(raw[1]),
            "price_high": _to_float(raw[2]),
            "price_low": _to_float(raw[3]),
            "volume_24h": _to_float(raw[5]),
        }

    # 指标名 -> 解析方法映射
    PARSER_MAP = {
        "rsi": "parse_rsi",
        "macd": "parse_macd",
        "bb": "parse_bb",
        "atr": "parse_atr",
        "supertrend": "parse_supertrend",
        "vwap": "parse_vwap",
        "cmf": "parse_cmf",
        "obv": "parse_obv",
        "kdj": "parse_kdj",
        "stoch-rsi": "parse_stoch_rsi",
        "mfi": "parse_mfi",
        "ema": "parse_ema",
        "top-long-short": "parse_top_long_short",
    }


# ============================================================
# 聪明钱分析器（使用V5 REST API）
# ============================================================

class SmartMoneyAnalyzer:
    """
    通过OKX V5 REST API获取聪明钱共识信号
    
    三层信号质量对比：
    1. 全量信号：所有交易员
    2. 高质量信号：胜率>=80% + PnL前20%
    3. 大资金信号：AUM前20%
    
    信号质量评估逻辑：
    - 三层方向一致 = 强共识（高权重）
    - 高质量与全量不一致 = 低质量共识（散户跟风，降权）
    - 大资金与全量不一致 = 大资金反向（最危险信号）
    """

    @staticmethod
    def _classify_direction(long_pct: Optional[float]) -> str:
        """根据多方占比判断方向"""
        if long_pct is None:
            return "unknown"
        if long_pct >= 0.55:
            return "long"
        elif long_pct <= 0.45:
            return "short"
        return "neutral"

    @staticmethod
    def analyze(symbol: str = "BTC") -> Dict:
        """
        获取聪明钱共识信号（三层质量对比）
        并行调用三个筛选层级的signal，对比方向一致性
        """
        result = {
            "smart_money_long_pct": None,
            "smart_money_short_pct": None,
            "smart_money_weighted_long": None,
            "smart_money_weighted_short": None,
            "smart_money_direction": None,
            "smart_money_consensus": None,
            "smart_money_long_entry": None,
            "smart_money_short_entry": None,
            "smart_money_traders_count": None,
            "smart_money_vs1h": None,
            "smart_money_vs24h": None,
            "smart_money_vs7d": None,
            "smart_money_net_notional": None,
            "smart_money_total_notional_vs24h": None,
            "smart_money_elite_long_pct": None,
            "smart_money_whale_long_pct": None,
            "smart_money_avg_long_wr": None,
            "smart_money_avg_short_wr": None,
            "smart_money_quality_consensus": None,
            "smart_money_quality_score": None,
        }

        try:
            # 并行调用三层信号
            with ThreadPoolExecutor(max_workers=3) as pool:
                f_all = pool.submit(OKXRestClient.get_smart_signal, symbol)
                f_elite = pool.submit(OKXRestClient.get_smart_signal_elite, symbol)
                f_whale = pool.submit(OKXRestClient.get_smart_signal_whale, symbol)

                signal_all = f_all.result(timeout=12)
                signal_elite = f_elite.result(timeout=12)
                signal_whale = f_whale.result(timeout=12)

            if not signal_all:
                logger.warning(f"SmartMoney signal empty for {symbol}")
                return result

            # === 解析全量信号 ===
            long_ratio = _to_float(signal_all.get("longRatio"))
            short_ratio = _to_float(signal_all.get("shortRatio"))
            weighted_long = _to_float(signal_all.get("weightedLongRatio"))
            weighted_short = _to_float(signal_all.get("weightedShortRatio"))
            vs1h = _to_float(signal_all.get("vs1h"))
            vs24h = _to_float(signal_all.get("vs24h"))
            vs7d = _to_float(signal_all.get("vs7d"))
            long_entry = _to_float(signal_all.get("smartMoneyLongAvgEntry"))
            short_entry = _to_float(signal_all.get("smartMoneyShortAvgEntry"))
            traders_count = signal_all.get("tradersWithPosition")
            net_notional = _to_float(signal_all.get("netNotionalUsdt"))
            total_notional_vs24h = _to_float(signal_all.get("totalNotionalVs24h"))
            avg_long_wr = _to_float(signal_all.get("avgLongWinRate"))
            avg_short_wr = _to_float(signal_all.get("avgShortWinRate"))

            result["smart_money_long_pct"] = long_ratio
            result["smart_money_short_pct"] = short_ratio
            result["smart_money_weighted_long"] = weighted_long
            result["smart_money_weighted_short"] = weighted_short
            result["smart_money_long_entry"] = long_entry
            result["smart_money_short_entry"] = short_entry
            result["smart_money_traders_count"] = int(traders_count) if traders_count else None
            result["smart_money_vs1h"] = vs1h
            result["smart_money_vs24h"] = vs24h
            result["smart_money_vs7d"] = vs7d
            result["smart_money_net_notional"] = net_notional
            result["smart_money_total_notional_vs24h"] = total_notional_vs24h
            result["smart_money_avg_long_wr"] = avg_long_wr
            result["smart_money_avg_short_wr"] = avg_short_wr

            # === 解析高质量信号 ===
            elite_long = None
            if signal_elite:
                elite_long = _to_float(signal_elite.get("weightedLongRatio")) or _to_float(signal_elite.get("longRatio"))
                result["smart_money_elite_long_pct"] = elite_long
                # 高质量层的胜率更有参考价值
                elite_long_wr = _to_float(signal_elite.get("avgLongWinRate"))
                elite_short_wr = _to_float(signal_elite.get("avgShortWinRate"))
                if elite_long_wr:
                    result["smart_money_avg_long_wr"] = elite_long_wr
                if elite_short_wr:
                    result["smart_money_avg_short_wr"] = elite_short_wr

            # === 解析大资金信号 ===
            whale_long = None
            if signal_whale:
                whale_long = _to_float(signal_whale.get("weightedLongRatio")) or _to_float(signal_whale.get("longRatio"))
                result["smart_money_whale_long_pct"] = whale_long

            # === 判断方向和共识强度 ===
            # 使用加权多方占比作主方向
            ratio = weighted_long if weighted_long is not None else long_ratio
            if ratio is not None:
                if ratio >= 0.7:
                    result["smart_money_direction"] = "long"
                    result["smart_money_consensus"] = "强共识做多"
                elif ratio >= 0.55:
                    result["smart_money_direction"] = "long"
                    result["smart_money_consensus"] = "中共识做多"
                elif ratio <= 0.3:
                    result["smart_money_direction"] = "short"
                    result["smart_money_consensus"] = "强共识做空"
                elif ratio <= 0.45:
                    result["smart_money_direction"] = "short"
                    result["smart_money_consensus"] = "中共识做空"
                else:
                    result["smart_money_direction"] = "neutral"
                    result["smart_money_consensus"] = "多空分歧"

            # === 三层信号质量评估 ===
            dir_all = SmartMoneyAnalyzer._classify_direction(weighted_long or long_ratio)
            dir_elite = SmartMoneyAnalyzer._classify_direction(elite_long)
            dir_whale = SmartMoneyAnalyzer._classify_direction(whale_long)

            # 计算一致性
            directions = [d for d in [dir_all, dir_elite, dir_whale] if d != "unknown"]
            if len(directions) >= 2:
                unique = set(directions)
                if len(unique) == 1 and "neutral" not in unique:
                    result["smart_money_quality_consensus"] = "strong"
                elif len(unique) <= 2 and "neutral" in unique:
                    result["smart_money_quality_consensus"] = "weak"
                elif dir_whale != "unknown" and dir_whale != dir_all:
                    result["smart_money_quality_consensus"] = "divergent"  # 大资金反向！
                else:
                    result["smart_money_quality_consensus"] = "weak"
            else:
                result["smart_money_quality_consensus"] = "unknown"

            # 计算质量评分 (0-1)
            score = 0.5  # 基础分
            # 三层一致 +0.3
            if result["smart_money_quality_consensus"] == "strong":
                score += 0.3
            elif result["smart_money_quality_consensus"] == "divergent":
                score -= 0.3
            # 高胜率加分
            if avg_long_wr and avg_long_wr > 0.8:
                score += 0.1
            elif avg_short_wr and avg_short_wr > 0.8:
                score += 0.1
            # 趋势加强加分（vs24h和vs7d同向）
            if vs24h is not None and vs7d is not None:
                if (vs24h > 0 and vs7d > 0) or (vs24h < 0 and vs7d < 0):
                    score += 0.1  # 趋势持续加强
            # 限制范围
            result["smart_money_quality_score"] = max(0.0, min(1.0, score))

        except Exception as e:
            logger.error(f"SmartMoney analyze error for {symbol}: {e}")

        return result


# ============================================================
# 数据聚合器：并行拉取所有数据
# ============================================================

class DataAggregator:
    """并行调用所有OKX CLI命令+V5 API，聚合成标准格式"""

    def __init__(self, max_workers: int = MAX_WORKERS):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

    def fetch_full(self, symbol: str, timeframe: str = DEFAULT_TIMEFRAME,
                   include_smart_money: bool = True) -> Dict[str, Any]:
        """
        并行拉取一个币种的全部数据，返回标准格式

        Args:
            symbol: 币种名称 (BTC/ETH/SOL/DOGE/OKB)
            timeframe: 时间框架 (1H/4H/1Dutc)
            include_smart_money: 是否包含聪明钱数据

        Returns:
            标准格式数据字典
        """
        start_time = time.time()
        inst_id = f"{symbol}-USDT-SWAP"
        data = empty_standard_data(symbol)
        errors = []

        # 构建并行任务
        futures = {}

        # 1. K线数据（价格）
        futures["candle"] = self.executor.submit(
            OKXCliExecutor.get_candles, inst_id, timeframe, 1
        )

        # 2. 所有技术指标
        for indicator in INDICATORS:
            futures[f"ind_{indicator}"] = self.executor.submit(
                OKXCliExecutor.get_indicator, inst_id, indicator, timeframe
            )

        # 3. 资金费率
        futures["funding"] = self.executor.submit(
            OKXCliExecutor.get_funding_rate, inst_id
        )

        # 4. 持仓量
        futures["oi"] = self.executor.submit(
            OKXCliExecutor.get_oi_history, inst_id
        )

        # 5. 聪明钱（V5 REST API，一次调用）
        if include_smart_money:
            futures["smart_signal"] = self.executor.submit(
                SmartMoneyAnalyzer.analyze, symbol
            )

        # 收集结果
        results = {}
        for key, future in futures.items():
            try:
                results[key] = future.result(timeout=CLI_TIMEOUT + 5)
            except Exception as e:
                results[key] = None
                errors.append(f"{key}: {str(e)}")

        # 解析K线
        candle_data = results.get("candle")
        if candle_data and len(candle_data) > 0:
            parsed = DataParser.parse_candle(candle_data[0])
            data.update(parsed)

        # 解析技术指标
        for indicator in INDICATORS:
            raw = results.get(f"ind_{indicator}")
            parser_name = DataParser.PARSER_MAP.get(indicator)
            if parser_name and raw:
                parsed = getattr(DataParser, parser_name)(raw)
                data.update(parsed)

        # 解析资金费率
        funding_raw = results.get("funding")
        if funding_raw:
            data.update(DataParser.parse_funding_rate(funding_raw))

        # 解析持仓量
        oi_raw = results.get("oi")
        if oi_raw:
            data.update(DataParser.parse_oi(oi_raw))

        # 解析聪明钱
        if include_smart_money:
            smart_data = results.get("smart_signal")
            if smart_data:
                data.update(smart_data)

        # 元数据
        data["timestamp"] = int(time.time())
        data["fetch_time_ms"] = int((time.time() - start_time) * 1000)
        data["errors"] = errors

        return data

    def fetch_multi_timeframe(self, symbol: str) -> Dict[str, Any]:
        """
        拉取多时间框架数据（1H/4H/1D），用于多TF共振判断
        主时间框架为4H（完整数据），1H和1D只拉关键指标
        """
        # 主数据：4H完整
        data = self.fetch_full(symbol, "4H", include_smart_money=True)

        # 辅助时间框架：只拉方向性指标
        for tf in ["1H", "1Dutc"]:
            tf_data = {}
            tf_key = f"tf_{tf.replace('utc', '').lower()}"

            # 并行拉取关键指标
            tf_futures = {}
            inst_id = f"{symbol}-USDT-SWAP"
            for ind in ["rsi", "macd", "supertrend", "ema"]:
                tf_futures[ind] = self.executor.submit(
                    OKXCliExecutor.get_indicator, inst_id, ind, tf
                )

            for ind, future in tf_futures.items():
                try:
                    raw = future.result(timeout=CLI_TIMEOUT + 5)
                    parser_name = DataParser.PARSER_MAP.get(ind)
                    if parser_name and raw:
                        parsed = getattr(DataParser, parser_name)(raw)
                        tf_data.update(parsed)
                except Exception:
                    pass

            # 判断该时间框架方向
            direction = _judge_tf_direction(tf_data)
            tf_data["direction"] = direction
            data[tf_key] = tf_data

        return data

    def shutdown(self):
        """关闭线程池"""
        self.executor.shutdown(wait=False)


# ============================================================
# 缓存管理器：后台持续刷新
# ============================================================

class CacheManager:
    """
    后台缓存管理器
    - 每5分钟自动刷新所有监控币种
    - 用户请求时直接读缓存，秒回
    - 容错降级：刷新失败保留上次有效数据
    """

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}  # {symbol: standard_data}
        self._cache_time: Dict[str, float] = {}  # {symbol: timestamp}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._aggregator = DataAggregator()

    def start(self):
        """启动后台刷新线程"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._thread.start()
        logger.info("CacheManager started, monitoring: %s", SYMBOLS)

    def stop(self):
        """停止后台刷新"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._aggregator.shutdown()
        logger.info("CacheManager stopped")

    def get(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取缓存数据（秒回）"""
        with self._lock:
            cached = self._cache.get(symbol.upper())
            if cached:
                return deepcopy(cached)
        return None

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """获取所有币种缓存数据"""
        with self._lock:
            return {k: deepcopy(v) for k, v in self._cache.items()}

    def force_refresh(self, symbol: str) -> Dict[str, Any]:
        """强制刷新某个币种（同步，用于调试）"""
        data = self._aggregator.fetch_multi_timeframe(symbol)
        with self._lock:
            self._cache[symbol.upper()] = data
            self._cache_time[symbol.upper()] = time.time()
        return data

    def is_fresh(self, symbol: str) -> bool:
        """检查缓存是否新鲜"""
        with self._lock:
            t = self._cache_time.get(symbol.upper(), 0)
            return (time.time() - t) < CACHE_TTL

    @property
    def status(self) -> Dict:
        """缓存状态摘要"""
        with self._lock:
            return {
                "running": self._running,
                "symbols": list(self._cache.keys()),
                "cache_ages": {
                    k: int(time.time() - v)
                    for k, v in self._cache_time.items()
                },
                "total_cached": len(self._cache),
            }

    def _refresh_loop(self):
        """后台刷新循环"""
        # 首次立即刷新
        self._refresh_all()

        while self._running:
            time.sleep(REFRESH_INTERVAL)
            if self._running:
                self._refresh_all()

    def _refresh_all(self):
        """刷新所有监控币种"""
        logger.info("Refreshing all symbols: %s", SYMBOLS)
        for symbol in SYMBOLS:
            try:
                data = self._aggregator.fetch_multi_timeframe(symbol)
                # 只有拿到有效数据才更新缓存（容错降级）
                if data and data.get("price") is not None:
                    with self._lock:
                        self._cache[symbol.upper()] = data
                        self._cache_time[symbol.upper()] = time.time()
                    logger.info(
                        "✓ %s refreshed: price=%.1f, RSI=%.1f, EMA5=%.1f, SM=%s, fetch=%dms",
                        symbol, data["price"] or 0,
                        data.get("rsi_14") or 0,
                        data.get("ema_5") or 0,
                        data.get("smart_money_consensus") or "N/A",
                        data.get("fetch_time_ms", 0)
                    )
                elif data:
                    with self._lock:
                        old = self._cache.get(symbol.upper(), {})
                        if old.get("price"):
                            data["price"] = old["price"]
                        self._cache[symbol.upper()] = data
                        self._cache_time[symbol.upper()] = time.time()
                    logger.warning("⚠ %s partial refresh (no price)", symbol)
                else:
                    logger.warning("✗ %s refresh failed, keeping old cache", symbol)
            except Exception as e:
                logger.error("✗ %s refresh exception: %s", symbol, e)


# ============================================================
# 公开API接口（供策略引擎调用）
# ============================================================

# 全局单例
_cache_manager: Optional[CacheManager] = None


def init(symbols: List[str] = None):
    """
    初始化数据接口层，启动后台缓存

    Args:
        symbols: 监控币种列表，默认 ["BTC", "ETH", "SOL", "DOGE", "OKB"]
    """
    global _cache_manager, SYMBOLS
    if symbols:
        SYMBOLS = [s.upper() for s in symbols]
    _cache_manager = CacheManager()
    _cache_manager.start()


def get_data(symbol: str) -> Optional[Dict[str, Any]]:
    """
    获取标准格式数据（秒回，从缓存读取）

    这是策略引擎的唯一数据入口。
    引擎不需要知道数据从哪来，只需要调用这个函数。
    """
    if _cache_manager is None:
        logger.error("DataAPI not initialized! Call init() first.")
        return None
    return _cache_manager.get(symbol.upper())


def get_all_data() -> Dict[str, Dict[str, Any]]:
    """获取所有监控币种的数据"""
    if _cache_manager is None:
        return {}
    return _cache_manager.get_all()


def force_refresh(symbol: str) -> Dict[str, Any]:
    """强制刷新某个币种（同步调用，用于调试或紧急更新）"""
    if _cache_manager is None:
        init()
    return _cache_manager.force_refresh(symbol.upper())


def get_status() -> Dict:
    """获取数据接口层状态"""
    if _cache_manager is None:
        return {"running": False, "error": "Not initialized"}
    return _cache_manager.status


def shutdown():
    """关闭数据接口层"""
    global _cache_manager
    if _cache_manager:
        _cache_manager.stop()
        _cache_manager = None


# ============================================================
# 工具函数
# ============================================================

def _to_float(val) -> Optional[float]:
    """安全转换为float"""
    if val is None or val == "" or val == "N/A":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _judge_tf_direction(tf_data: Dict) -> str:
    """
    根据时间框架的关键指标判断方向
    RSI > 55 且 MACD DIF > DEA 且 SuperTrend buy → long
    RSI < 45 且 MACD DIF < DEA 且 SuperTrend sell → short
    EMA5 > EMA20 → bullish加分
    其他 → neutral
    """
    rsi = tf_data.get("rsi_14")
    macd_dif = tf_data.get("macd_dif")
    macd_dea = tf_data.get("macd_dea")
    st_dir = tf_data.get("supertrend_direction")
    ema5 = tf_data.get("ema_5")
    ema20 = tf_data.get("ema_20")

    bullish_count = 0
    bearish_count = 0

    if rsi is not None:
        if rsi > 55:
            bullish_count += 1
        elif rsi < 45:
            bearish_count += 1

    if macd_dif is not None and macd_dea is not None:
        if macd_dif > macd_dea:
            bullish_count += 1
        else:
            bearish_count += 1

    if st_dir:
        if st_dir == "buy":
            bullish_count += 1
        elif st_dir == "sell":
            bearish_count += 1

    if ema5 is not None and ema20 is not None:
        if ema5 > ema20:
            bullish_count += 1
        else:
            bearish_count += 1

    if bullish_count >= 3:
        return "long"
    elif bearish_count >= 3:
        return "short"
    elif bullish_count >= 2:
        return "long"
    elif bearish_count >= 2:
        return "short"
    return "neutral"


# ============================================================
# 独立运行测试
# ============================================================

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("H V3 数据接口层 v3.2 - 独立测试")
    print("=" * 60)

    # 单次拉取测试
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    print(f"\n[测试1] 单次拉取 {symbol} 全量数据...")

    aggregator = DataAggregator()
    data = aggregator.fetch_multi_timeframe(symbol)

    print(f"\n--- {symbol} 标准格式数据 (v3.2) ---")
    print(f"价格: {data.get('price')}")
    print(f"RSI(14): {data.get('rsi_14')}")
    print(f"MACD: DIF={data.get('macd_dif')} DEA={data.get('macd_dea')} HIST={data.get('macd_hist')}")
    print(f"BB: Upper={data.get('bb_upper')} Mid={data.get('bb_middle')} Lower={data.get('bb_lower')}")
    print(f"ATR(14): {data.get('atr_14')}")
    print(f"SuperTrend: {data.get('supertrend_value')} ({data.get('supertrend_direction')})")
    print(f"EMA: EMA5={data.get('ema_5')} EMA20={data.get('ema_20')}")
    print(f"VWAP: {data.get('vwap')}")
    print(f"CMF: {data.get('cmf')}")
    print(f"OBV: {data.get('obv')} (MA: {data.get('obv_ma')})")
    print(f"KDJ: K={data.get('kdj_k')} D={data.get('kdj_d')} J={data.get('kdj_j')}")
    print(f"StochRSI: {data.get('stoch_rsi')} K={data.get('stoch_rsi_k')} D={data.get('stoch_rsi_d')}")
    print(f"MFI: {data.get('mfi')}")
    print(f"资金费率: {data.get('funding_rate')}")
    print(f"持仓量: ${data.get('oi_usd'):,.0f}" if data.get('oi_usd') else "持仓量: N/A")
    print(f"OI变化: {data.get('oi_change_pct')}%")
    print(f"多空比: Long={data.get('long_ratio')} Short={data.get('short_ratio')}")
    print(f"\n--- 聪明钱 (V5 REST API) ---")
    print(f"聪明钱方向: {data.get('smart_money_consensus')} ({data.get('smart_money_direction')})")
    print(f"多方占比: {data.get('smart_money_long_pct')}")
    print(f"加权多方: {data.get('smart_money_weighted_long')}")
    print(f"多方入场价: {data.get('smart_money_long_entry')}")
    print(f"空方入场价: {data.get('smart_money_short_entry')}")
    print(f"持仓交易员: {data.get('smart_money_traders_count')}")
    print(f"1h变化: {data.get('smart_money_vs1h')}")
    print(f"24h变化: {data.get('smart_money_vs24h')}")
    print(f"\n--- 多时间框架 ---")
    print(f"1H: {data.get('tf_1h', {}).get('direction')} (EMA5={data.get('tf_1h', {}).get('ema_5')})")
    print(f"1D: {data.get('tf_1d', {}).get('direction')} (EMA5={data.get('tf_1d', {}).get('ema_5')})")
    print(f"\n耗时: {data.get('fetch_time_ms')}ms")
    print(f"错误: {data.get('errors')}")
    print(f"数据源: {data.get('data_source')}")

    aggregator.shutdown()
    print("\n✓ 测试完成")
