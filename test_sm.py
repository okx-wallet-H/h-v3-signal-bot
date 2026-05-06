#!/usr/bin/env python3.11
from h_v3_mcp_smartmoney import start_smart_money_service, get_smart_money_summary
import time

start_smart_money_service()
time.sleep(15)

symbols = ["BTC", "ETH", "SOL", "DOGE", "OKB"]
for sym in symbols:
    r = get_smart_money_summary(sym)
    txt = r.get("summary_text", "N/A")
    print(f"  {sym}: {txt}")
