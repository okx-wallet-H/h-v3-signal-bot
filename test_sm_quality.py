#!/usr/bin/env python3
"""测试三层聪明钱信号质量对比 - 带OKX API鉴权"""
import requests
import json
import time
import hmac
import hashlib
import base64
from datetime import datetime, timezone

# OKX API credentials
API_KEY = "YOUR_OKX_API_KEY"
SECRET_KEY = "YOUR_OKX_SECRET_KEY"
PASSPHRASE = "YOUR_OKX_PASSPHRASE"

def get_okx_headers(method, path, body=""):
    """生成OKX API鉴权headers"""
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + \
                f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    message = timestamp + method + path + body
    signature = base64.b64encode(
        hmac.new(SECRET_KEY.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json"
    }

BASE_URL = "https://www.okx.com"

def fetch_signal(params):
    """获取聪明钱信号"""
    path = "/api/v5/journal/smartmoney/signal?" + "&".join(f"{k}={v}" for k, v in params.items())
    headers = get_okx_headers("GET", path)
    r = requests.get(BASE_URL + path, headers=headers, timeout=10)
    return r.json()

def fetch_signal_history(params):
    """获取信号历史"""
    path = "/api/v5/journal/smartmoney/signal-history?" + "&".join(f"{k}={v}" for k, v in params.items())
    headers = get_okx_headers("GET", path)
    r = requests.get(BASE_URL + path, headers=headers, timeout=10)
    return r.json()

# 三层筛选
layers = {
    "全量信号（所有交易员）": {"instCcy": "BTC"},
    "高质量信号（WR>=80% + PnL TOP20%）": {"instCcy": "BTC", "winRatio": "WR_GE_80", "pnl": "PNL_TOP20"},
    "大资金信号（AUM TOP20%）": {"instCcy": "BTC", "asset": "AUM_TOP20"},
}

for name, params in layers.items():
    try:
        data = fetch_signal(params)
        if data.get("code") == "0" and data.get("data"):
            d = data["data"][0]
            long_r = float(d.get("longRatio", 0))
            weighted = float(d.get("weightedLongRatio", 0))
            avg_long_wr = d.get("avgLongWinRatio", "N/A")
            avg_short_wr = d.get("avgShortWinRatio", "N/A")
            traders = d.get("tradersWithPosition", 0)
            long_t = d.get("longTraders", 0)
            short_t = d.get("shortTraders", 0)
            net = d.get("netNotionalUsdt", "0")
            vs1h = d.get("vs1h", "0")
            vs24h = d.get("vs24h", "0")
            vs7d = d.get("vs7d", "0")
            long_entry = d.get("smartMoneyLongAvgEntry", "N/A")
            short_entry = d.get("smartMoneyShortAvgEntry", "N/A")
            pool = d.get("tradersTotal", 0)
            
            direction = "LONG" if long_r > 0.55 else "SHORT" if long_r < 0.45 else "NEUTRAL"
            
            print(f"\n{'='*50}")
            print(f"  {name}")
            print(f"{'='*50}")
            print(f"  方向: {direction} (多方{long_r:.1%})")
            print(f"  加权多方: {weighted:.1%}")
            print(f"  多方胜率: {avg_long_wr} | 空方胜率: {avg_short_wr}")
            print(f"  交易员: {traders}人 (多{long_t}/空{short_t})")
            print(f"  净名义: ${float(net):,.0f}")
            print(f"  趋势: 1h={vs1h} | 24h={vs24h} | 7d={vs7d}")
            if long_entry != "N/A" and long_entry:
                print(f"  入场价: 多方${float(long_entry):,.1f} | 空方${float(short_entry):,.1f}")
            print(f"  池大小: {pool}")
        else:
            print(f"\n{name}: Error - {data.get('msg', data)}")
    except Exception as e:
        print(f"\n{name}: Exception - {e}")

# 测试signal-history
print(f"\n\n{'='*50}")
print("  信号历史（最近6小时趋势）")
print(f"{'='*50}")
try:
    data = fetch_signal_history({"instId": "BTC-USDT-SWAP", "granularity": "1h", "limit": "6"})
    if data.get("code") == "0" and data.get("data"):
        for item in data["data"][:6]:
            lr = float(item.get("longRatio", 0))
            wlr = float(item.get("weightedLongRatio", 0))
            traders = item.get("tradersWithPosition", 0)
            ts = item.get("dataVersion", "")
            print(f"  {ts}: 多方{lr:.1%} (加权{wlr:.1%}) | {traders}人")
    else:
        print(f"  Error: {data.get('msg', data)}")
except Exception as e:
    print(f"  Exception: {e}")
