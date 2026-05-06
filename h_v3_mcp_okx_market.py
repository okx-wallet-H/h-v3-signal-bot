"""
H_V3 MCP OKX Market Server
============================
基于 MCP 协议封装 OKX V5 行情 API，提供标准化的行情数据服务。
任何 MCP Client 均可无缝调用本服务获取实时行情和 K 线数据。

支持的 Tools:
  - get_ticker: 获取单个交易对的实时行情
  - get_tickers: 批量获取多个交易对的实时行情
  - get_klines: 获取 K 线数据
  - get_orderbook: 获取深度数据
  - get_funding_rate: 获取资金费率

协议版本: OKX V5
MCP 传输: stdio (可扩展为 SSE)
"""

import json
import urllib.request
import urllib.error
from typing import Any
from mcp.server.fastmcp import FastMCP

# ============================================================
# MCP Server 初始化
# ============================================================

mcp = FastMCP("H_V3 OKX Market")

# ============================================================
# OKX V5 API 基础配置
# ============================================================

OKX_BASE_URL = "https://www.okx.com"
OKX_API_VERSION = "/api/v5"

# 标准交易对映射表（可通过配置扩展）
INSTRUMENT_MAP = {
    "BTC": "BTC-USDT-SWAP",
    "ETH": "ETH-USDT-SWAP",
    "SOL": "SOL-USDT-SWAP",
    "DOGE": "DOGE-USDT-SWAP",
    "OKB": "OKB-USDT-SWAP",
}

# K线周期映射
TIMEFRAME_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1H": "1H", "2H": "2H", "4H": "4H",
    "6H": "6H", "12H": "12H", "1D": "1D", "1W": "1W", "1M": "1M",
}


# ============================================================
# 内部工具函数
# ============================================================

def _okx_request(endpoint: str, params: dict = None) -> dict:
    """
    统一的 OKX V5 API 请求方法。
    返回标准化的响应结构：{"code": "0", "data": [...], "msg": ""}
    """
    url = f"{OKX_BASE_URL}{OKX_API_VERSION}{endpoint}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        url = f"{url}?{query}"

    req = urllib.request.Request(url, headers={
        "User-Agent": "H_V3/3.0.0",
        "Accept": "application/json",
    })

    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        return data
    except urllib.error.HTTPError as e:
        return {"code": str(e.code), "data": [], "msg": f"HTTP {e.code}: {e.reason}"}
    except urllib.error.URLError as e:
        return {"code": "-1", "data": [], "msg": f"Network error: {e.reason}"}
    except Exception as e:
        return {"code": "-1", "data": [], "msg": str(e)}


def _resolve_inst_id(symbol: str) -> str:
    """
    将简写符号（如 BTC）解析为完整的 OKX instId（如 BTC-USDT-SWAP）。
    如果传入的已经是完整 instId 格式，则直接返回。
    """
    symbol_upper = symbol.upper().strip()
    if "-" in symbol_upper:
        return symbol_upper
    return INSTRUMENT_MAP.get(symbol_upper, f"{symbol_upper}-USDT-SWAP")


# ============================================================
# MCP Tools 定义
# ============================================================

@mcp.tool()
def get_ticker(symbol: str) -> dict:
    """
    获取单个交易对的实时行情数据。

    Args:
        symbol: 交易对符号，支持简写（BTC）或完整格式（BTC-USDT-SWAP）

    Returns:
        包含以下字段的字典：
        - inst_id: 交易对 ID
        - last_price: 最新价
        - open_24h: 24h 开盘价
        - high_24h: 24h 最高价
        - low_24h: 24h 最低价
        - change_24h: 24h 涨跌幅（百分比）
        - volume_24h: 24h 成交量（币）
        - volume_24h_usdt: 24h 成交额（USDT）
        - timestamp: 数据时间戳（毫秒）
    """
    inst_id = _resolve_inst_id(symbol)
    resp = _okx_request("/market/ticker", {"instId": inst_id})

    if resp["code"] != "0" or not resp["data"]:
        return {"error": True, "message": resp["msg"], "inst_id": inst_id}

    d = resp["data"][0]
    open_24h = float(d.get("open24h", 0))
    last_price = float(d.get("last", 0))
    change_pct = ((last_price - open_24h) / open_24h * 100) if open_24h > 0 else 0

    return {
        "error": False,
        "inst_id": inst_id,
        "last_price": last_price,
        "open_24h": open_24h,
        "high_24h": float(d.get("high24h", 0)),
        "low_24h": float(d.get("low24h", 0)),
        "change_24h": round(change_pct, 2),
        "volume_24h": float(d.get("vol24h", 0)),
        "volume_24h_usdt": float(d.get("volCcy24h", 0)),
        "bid_price": float(d.get("bidPx", 0)),
        "ask_price": float(d.get("askPx", 0)),
        "timestamp": int(d.get("ts", 0)),
    }


@mcp.tool()
def get_tickers(symbols: list[str] = None) -> dict:
    """
    批量获取多个交易对的实时行情。

    Args:
        symbols: 交易对列表，如 ["BTC", "ETH", "SOL"]。
                 如果不传，默认返回所有已配置的交易对。

    Returns:
        包含所有交易对行情数据的字典，key 为符号名。
    """
    if symbols is None:
        symbols = list(INSTRUMENT_MAP.keys())

    results = {}
    for sym in symbols:
        results[sym.upper()] = get_ticker(sym)

    return results


@mcp.tool()
def get_klines(symbol: str, timeframe: str = "4H", limit: int = 100) -> dict:
    """
    获取 K 线（蜡烛图）数据。

    Args:
        symbol: 交易对符号，支持简写（BTC）或完整格式（BTC-USDT-SWAP）
        timeframe: K线周期，支持 1m/5m/15m/30m/1H/2H/4H/6H/12H/1D/1W/1M
        limit: 返回的 K 线数量，最大 300

    Returns:
        包含以下字段的字典：
        - inst_id: 交易对 ID
        - timeframe: K线周期
        - count: 实际返回的 K 线数量
        - candles: K线数组，每根包含 [timestamp, open, high, low, close, volume]
    """
    inst_id = _resolve_inst_id(symbol)
    bar = TIMEFRAME_MAP.get(timeframe, timeframe)
    limit = min(max(1, limit), 300)

    resp = _okx_request("/market/candles", {
        "instId": inst_id,
        "bar": bar,
        "limit": str(limit),
    })

    if resp["code"] != "0" or not resp["data"]:
        return {"error": True, "message": resp["msg"], "inst_id": inst_id}

    candles = []
    for c in resp["data"]:
        candles.append({
            "timestamp": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        })

    # OKX 返回的是倒序（最新在前），我们翻转为正序（最旧在前）
    candles.reverse()

    return {
        "error": False,
        "inst_id": inst_id,
        "timeframe": bar,
        "count": len(candles),
        "candles": candles,
    }


@mcp.tool()
def get_orderbook(symbol: str, depth: int = 5) -> dict:
    """
    获取交易对的深度数据（买卖盘口）。

    Args:
        symbol: 交易对符号
        depth: 深度档位数量（1-400），默认 5 档

    Returns:
        包含 bids（买盘）和 asks（卖盘）的字典，每档包含 [价格, 数量]
    """
    inst_id = _resolve_inst_id(symbol)
    depth = min(max(1, depth), 400)

    resp = _okx_request("/market/books", {
        "instId": inst_id,
        "sz": str(depth),
    })

    if resp["code"] != "0" or not resp["data"]:
        return {"error": True, "message": resp["msg"], "inst_id": inst_id}

    book = resp["data"][0]
    bids = [[float(b[0]), float(b[1])] for b in book.get("bids", [])]
    asks = [[float(a[0]), float(a[1])] for a in book.get("asks", [])]

    return {
        "error": False,
        "inst_id": inst_id,
        "bids": bids,
        "asks": asks,
        "timestamp": int(book.get("ts", 0)),
    }


@mcp.tool()
def get_funding_rate(symbol: str) -> dict:
    """
    获取永续合约的当前资金费率和预测资金费率。

    Args:
        symbol: 交易对符号

    Returns:
        包含当前费率、预测费率和下次结算时间的字典
    """
    inst_id = _resolve_inst_id(symbol)

    resp = _okx_request("/public/funding-rate", {"instId": inst_id})

    if resp["code"] != "0" or not resp["data"]:
        return {"error": True, "message": resp["msg"], "inst_id": inst_id}

    d = resp["data"][0]
    return {
        "error": False,
        "inst_id": inst_id,
        "funding_rate": float(d.get("fundingRate", 0)),
        "next_funding_rate": float(d.get("nextFundingRate", 0)),
        "funding_time": int(d.get("fundingTime", 0)),
        "next_funding_time": int(d.get("nextFundingTime", 0)),
    }


# ============================================================
# MCP Resources 定义（提供静态配置信息）
# ============================================================

@mcp.resource("config://instruments")
def get_instruments_config() -> str:
    """返回当前已配置的交易对映射表"""
    return json.dumps(INSTRUMENT_MAP, indent=2)


@mcp.resource("config://timeframes")
def get_timeframes_config() -> str:
    """返回支持的 K 线周期列表"""
    return json.dumps(list(TIMEFRAME_MAP.keys()))


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
