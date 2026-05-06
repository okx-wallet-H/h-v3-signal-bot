"""测试 H_V3 AI MCP Server"""
import sys
import json
sys.path.insert(0, ".")

from h_v3_mcp_ai import chat, analyze_sentiment, list_providers

def main():
    print("=" * 60)
    print("  H_V3 AI MCP Server 测试")
    print("=" * 60)

    # 测试1: 列出提供商
    print("\n--- 可用 AI 提供商 ---")
    providers = list_providers()
    for key, info in providers.items():
        status = "✅ 激活" if info["is_active"] else ("🟢 可用" if info["available"] else "⚪ 未配置")
        print(f"  {info['name']}: {status} | 模型: {info['models']['default']}")

    # 测试2: 带引擎数据的对话
    print("\n--- AI 对话测试（带引擎数据） ---")
    engine_data = {
        "symbol": "ETH",
        "name": "以太坊",
        "direction": "long",
        "score": 3.0,
        "hurst": 0.896,
        "market_state": "强趋势",
        "rsi": 62.5,
        "ema_fast": 2363.75,
        "ema_slow": 2343.34,
        "macd_histogram": 2.24,
        "atr": 37.58,
        "entry_price": 2381.99,
        "tp_price": 2475.94,
        "sl_price": 2325.62,
        "risk_level": "中",
        "reason": "建议做多：EMA金叉, MACD柱为正, 赫斯特确认趋势",
    }
    result = chat("ETH现在能做多吗？", engine_data=engine_data)
    print(f"  模型: {result['provider']} / {result['model']}")
    print(f"  回答: {result['response'][:300]}")

    # 测试3: 情绪分析
    print("\n--- 情绪分析测试 ---")
    sentiment = analyze_sentiment("BTC", "BTC突破81000，市场情绪高涨")
    print(f"  评分: {sentiment['score']}")
    print(f"  标签: {sentiment['label']}")
    print(f"  总结: {sentiment.get('summary', '')[:100]}")

    print("\n" + "=" * 60)
    print("  测试完成!")
    print("=" * 60)

if __name__ == "__main__":
    main()
