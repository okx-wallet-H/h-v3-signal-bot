"""测试 H_V3 Engine MCP Server 的 scan_symbol 功能"""
import sys
import json
sys.path.insert(0, "/root/h_v3" if __name__ == "__main__" else ".")

from h_v3_mcp_engine import scan_symbol, calculate_hurst

def main():
    print("=" * 60)
    print("  H_V3 Engine MCP Server 测试")
    print("=" * 60)

    symbols = ["BTC", "ETH", "SOL"]
    for sym in symbols:
        print(f"\n--- {sym} 扫描结果 ---")
        result = scan_symbol(sym, "4H")
        if result.get("error"):
            print(f"  错误: {result['message']}")
            continue

        print(f"  方向: {result['direction']}")
        print(f"  评分: {result['score']}")
        print(f"  赫斯特: {result['hurst']} ({result['market_state']})")
        print(f"  RSI: {result['rsi']}")
        print(f"  EMA: fast={result['ema_fast']:.2f} slow={result['ema_slow']:.2f}")
        print(f"  MACD柱: {result['macd_histogram']}")
        print(f"  ATR: {result['atr']:.2f}")
        print(f"  入场: {result['entry_price']:,.2f}")
        print(f"  止盈: {result['tp_price']:,.2f}")
        print(f"  止损: {result['sl_price']:,.2f}")
        print(f"  风险: {result['risk_level']}")
        print(f"  理由: {result['reason']}")
        print(f"  因子: {result['factors']}")

    print("\n" + "=" * 60)
    print("  测试完成!")
    print("=" * 60)

if __name__ == "__main__":
    main()
