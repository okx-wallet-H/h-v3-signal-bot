#!/usr/bin/env python3.11
"""
H_V3 MCP Smart Money Server (v2 - 预缓存架构)
================================================
封装 OKX Smart Money（聪明钱）数据，提供标准 MCP Tools。

性能架构：
  - 后台守护线程每 5 分钟自动刷新所有币种的聪明钱数据
  - 用户请求时直接读内存缓存（0ms 延迟）
  - Bot 启动时异步预热，不阻塞主流程
  - CLI 调用失败时保留上一次有效数据

Tools:
  - get_smart_money_summary: 综合聪明钱摘要（引擎注入用，秒回）
  - get_top_traders: 获取聪明钱排行榜
  - get_smart_money_consensus: 获取指定币种多空共识
"""

import subprocess
import json
import os
import time
import threading
from typing import Optional

# ============================================================
# 配置
# ============================================================
OKX_API_KEY = os.environ.get("OKX_API_KEY", "YOUR_OKX_API_KEY")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "YOUR_OKX_SECRET_KEY")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "YOUR_OKX_PASSPHRASE")

SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "OKB"]

ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "OKX_API_KEY": OKX_API_KEY,
    "OKX_SECRET_KEY": OKX_SECRET_KEY,
    "OKX_PASSPHRASE": OKX_PASSPHRASE,
}

# 刷新间隔（秒）
REFRESH_INTERVAL = 300  # 5 分钟
CLI_TIMEOUT = 10  # 单次 CLI 超时


# ============================================================
# 全局缓存
# ============================================================
class SmartMoneyCache:
    """聪明钱数据缓存 — 后台定时刷新"""

    def __init__(self):
        self._data = {}  # {symbol: {direction, confidence, long_pct, ...}}
        self._traders = []  # Top 交易员列表
        self._last_refresh = 0
        self._lock = threading.Lock()
        self._running = False

    def get_summary(self, symbol: str) -> dict:
        """获取缓存中的聪明钱摘要（秒回）"""
        symbol = symbol.upper()
        with self._lock:
            if symbol in self._data:
                return self._data[symbol].copy()

        # 缓存中没有该币种数据
        return {
            "symbol": symbol,
            "direction": "neutral",
            "confidence": "low",
            "long_pct": 0,
            "short_pct": 0,
            "summary_text": "聪明钱数据预热中...",
            "cached": False,
        }

    def get_traders(self) -> dict:
        """获取缓存中的 Top 交易员列表"""
        with self._lock:
            if self._traders:
                return {"traders": self._traders.copy(), "cached": True}
        return {"traders": [], "error": "数据预热中"}

    def start_background_refresh(self):
        """启动后台刷新线程"""
        if self._running:
            return
        self._running = True
        t = threading.Thread(target=self._refresh_loop, daemon=True, name="SmartMoney-Refresh")
        t.start()
        print("[SmartMoney] 后台刷新线程已启动")

    def _refresh_loop(self):
        """后台循环刷新"""
        # 首次延迟 5 秒启动（让 Bot 主流程先跑起来）
        time.sleep(5)
        while self._running:
            try:
                self._do_refresh()
            except Exception as e:
                print(f"[SmartMoney] 刷新异常: {e}")
            time.sleep(REFRESH_INTERVAL)

    def _do_refresh(self):
        """执行一次完整刷新"""
        start = time.time()
        print(f"[SmartMoney] 开始刷新...")

        # 1. 获取 Top 交易员排行榜
        traders = self._fetch_top_traders()
        if traders:
            with self._lock:
                self._traders = traders

        # 2. 对每个币种计算共识
        for symbol in SYMBOLS:
            try:
                consensus = self._calc_consensus(symbol, traders[:3] if traders else [])
                with self._lock:
                    self._data[symbol] = consensus
            except Exception as e:
                print(f"[SmartMoney] {symbol} 共识计算失败: {e}")

        self._last_refresh = time.time()
        elapsed = time.time() - start
        print(f"[SmartMoney] 刷新完成 ({elapsed:.1f}s)")

    def _fetch_top_traders(self, limit: int = 10) -> list:
        """获取 Top 交易员"""
        data = _run_okx_cli(["smartmoney", "traders", "--limit", str(limit), "--period", "7", "--sortType", "pnl"])
        if isinstance(data, dict) and data.get("error"):
            print(f"[SmartMoney] 获取交易员失败: {data['error']}")
            return []
        if not isinstance(data, list):
            return []
        traders = []
        for t in data[:limit]:
            traders.append({
                "nickName": t.get("nickName", "N/A"),
                "authorId": t.get("authorId", ""),
                "pnl": float(t.get("pnl", 0)),
                "pnlRatio": float(t.get("pnlRatio", 0)),
                "maxDrawdown": float(t.get("maxDrawdown", 0)),
                "asset": float(t.get("asset", 0)),
            })
        return traders

    def _fetch_trader_trades(self, author_id: str) -> list:
        """获取交易员最近交易"""
        data = _run_okx_cli(["smartmoney", "trader", "--authorId", author_id, "--period", "7"])
        if isinstance(data, dict) and data.get("error"):
            return []
        trades = data.get("trades", [])
        return trades[:10]

    def _calc_consensus(self, symbol: str, top_traders: list) -> dict:
        """计算单个币种的聪明钱共识"""
        if not top_traders:
            return {
                "symbol": symbol,
                "direction": "neutral",
                "confidence": "low",
                "long_pct": 0,
                "short_pct": 0,
                "summary_text": "聪明钱数据不足",
                "cached": True,
            }

        long_count = 0
        short_count = 0
        long_names = []
        short_names = []

        for t in top_traders:
            trades = self._fetch_trader_trades(t["authorId"])
            # 找该币种的最近交易
            symbol_trades = [tr for tr in trades if symbol.upper() in tr.get("instId", "").upper()]
            if symbol_trades:
                side = symbol_trades[0].get("side", "")
                if side == "buy":
                    long_count += 1
                    long_names.append(t["nickName"])
                elif side == "sell":
                    short_count += 1
                    short_names.append(t["nickName"])

        total = long_count + short_count
        if total == 0:
            return {
                "symbol": symbol,
                "direction": "neutral",
                "confidence": "low",
                "long_pct": 0,
                "short_pct": 0,
                "summary_text": f"聪明钱在{symbol}无明显动作",
                "cached": True,
            }

        long_ratio = long_count / total
        short_ratio = short_count / total

        # 判断方向和信心度
        if long_ratio >= 0.7:
            direction, confidence = "long", "high"
        elif long_ratio >= 0.6:
            direction, confidence = "long", "medium"
        elif short_ratio >= 0.7:
            direction, confidence = "short", "high"
        elif short_ratio >= 0.6:
            direction, confidence = "short", "medium"
        else:
            direction, confidence = "neutral", "low"

        long_pct = round(long_ratio * 100, 1)
        short_pct = round(short_ratio * 100, 1)

        # 构建摘要
        dir_cn = {"long": "做多", "short": "做空", "neutral": "观望"}.get(direction, "")
        conf_cn = {"high": "强", "medium": "中", "low": "弱"}.get(confidence, "")
        summary = f"聪明钱{conf_cn}共识{dir_cn} (多{long_pct}%/空{short_pct}%)"

        return {
            "symbol": symbol,
            "direction": direction,
            "confidence": confidence,
            "long_pct": long_pct,
            "short_pct": short_pct,
            "long_traders": long_names,
            "short_traders": short_names,
            "summary_text": summary,
            "cached": True,
        }


# ============================================================
# 全局单例
# ============================================================
_cache = SmartMoneyCache()


def start_smart_money_service():
    """启动聪明钱后台服务（Bot 启动时调用一次）"""
    _cache.start_background_refresh()


# ============================================================
# CLI 工具函数
# ============================================================
def _run_okx_cli(args: list, timeout: int = CLI_TIMEOUT) -> dict:
    """调用 OKX CLI 并返回 JSON 结果"""
    try:
        result = subprocess.run(
            ["okx"] + args + ["--json"],
            capture_output=True,
            text=True,
            env=ENV,
            timeout=timeout,
        )
        if result.returncode != 0:
            return {"error": result.stderr.strip()[:100]}
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"error": "CLI超时"}
    except json.JSONDecodeError:
        return {"error": "JSON解析失败"}
    except Exception as e:
        return {"error": str(e)[:80]}


# ============================================================
# 对外 API（引擎和 Bot 调用这些）
# ============================================================
def get_smart_money_summary(symbol: str = "BTC") -> dict:
    """
    获取聪明钱综合摘要（秒回，读缓存）
    引擎 scan_symbol() 调用此函数注入聪明钱数据
    """
    return _cache.get_summary(symbol)


def get_top_traders(period: int = 7, limit: int = 10, sort_type: str = "pnl") -> dict:
    """获取 Top 交易员列表（读缓存）"""
    return _cache.get_traders()


def get_smart_money_consensus(symbol: str = "BTC", period: int = 7, top_n: int = 10) -> dict:
    """获取聪明钱共识（读缓存，等同于 get_smart_money_summary）"""
    return _cache.get_summary(symbol)


# ============================================================
# 直接运行测试
# ============================================================
if __name__ == "__main__":
    import sys

    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC"

    print(f"=== H_V3 Smart Money 测试: {symbol} ===")
    print(f"[模式] 预缓存架构 - 后台刷新\n")

    # 启动后台刷新
    start_smart_money_service()

    # 等待首次刷新完成
    print("[等待] 首次数据刷新中（约 30s）...")
    time.sleep(35)

    # 测试读取
    print("\n[结果] 从缓存读取:")
    for sym in SYMBOLS:
        data = get_smart_money_summary(sym)
        print(f"  {sym}: {data.get('summary_text', 'N/A')}")

    print("\n[Top 交易员]")
    traders = get_top_traders()
    for i, t in enumerate(traders.get("traders", [])[:3], 1):
        print(f"  #{i} {t['nickName']} | PnL: ${t['pnl']:,.0f}")

    print("\n[性能] 第二次读取（应该 <1ms）:")
    start = time.time()
    _ = get_smart_money_summary(symbol)
    print(f"  耗时: {(time.time()-start)*1000:.2f}ms")
