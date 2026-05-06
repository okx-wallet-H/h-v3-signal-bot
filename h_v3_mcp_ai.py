"""
H_V3 MCP AI Server
=====================
基于 MCP 协议封装大模型对话与情绪分析服务。
支持多模型热切换（Grok / DeepSeek / OpenAI），通过配置即可秒换。

支持的 Tools:
  - chat: 通用 AI 对话（注入引擎数据上下文）
  - analyze_sentiment: 舆情情绪分析
  - summarize_market: 市场综合总结

当前默认模型: Grok (grok-3-mini-fast)
MCP 传输: stdio
"""

import json
import urllib.request
import urllib.error
from typing import Any
from mcp.server.fastmcp import FastMCP

# ============================================================
# MCP Server 初始化
# ============================================================

mcp = FastMCP("H_V3 AI")

# ============================================================
# AI 模型配置（热切换核心）
# ============================================================

# 支持的模型提供商配置
AI_PROVIDERS = {
    "grok": {
        "name": "Grok",
        "base_url": "https://api.x.ai/v1/chat/completions",
        "api_key": "YOUR_GROK_API_KEY",
        "models": {
            "fast": "grok-3-mini-fast",
            "reasoning": "grok-3-mini-fast",
            "default": "grok-3-mini-fast",
        },
        "timeout": 60,
        "max_tokens": 1000,
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": "",  # 待配置
        "models": {
            "fast": "deepseek-chat",
            "reasoning": "deepseek-reasoner",
            "default": "deepseek-chat",
        },
        "timeout": 60,
        "max_tokens": 1000,
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1/chat/completions",
        "api_key": "",  # 待配置
        "models": {
            "fast": "gpt-4o-mini",
            "reasoning": "gpt-4o",
            "default": "gpt-4o-mini",
        },
        "timeout": 60,
        "max_tokens": 1000,
    },
}

# 当前激活的模型提供商（改这一行即可切换）
ACTIVE_PROVIDER = "grok"

# ============================================================
# 系统人设 Prompt
# ============================================================

SYSTEM_PROMPT_TRADER = """你是 H AI Agent，一个专业的合约策略交易员，对数据极度敏感。

你的身份：
- 你是 H AI Agent，隶属于 H Wallet 团队
- 你是一个真正的合约策略交易员，不是泛泛而谈的 AI 助手
- 你由 H_V3 策略引擎驱动，基于实时行情、多因子引擎和 AI 回测验证做决策

你的核心特质：
- 对数据极度敏感：价格波动、成交量变化、指标异动都逃不过你的眼睛
- 果断决策：该做多就做多，该止损就止损，绝不含糊
- 风控第一：每一笔建议都有明确的入场价、止盈价、止损价

你的职责：
1. 基于引擎提供的实时技术指标数据，给出精准的交易建议
2. 如果数据显示方向为 neutral，果断建议观望，说明为什么现在不是好时机
3. 止盈止损必须给出具体价位，精确到小数点
4. 提及赫斯特指数对应的市场状态（强趋势/弱趋势/震荡/随机游走）

回答风格：
- 像一个资深交易员在跟队友沟通，专业、果断、有底气
- 控制在 200 字以内，不废话
- 先给结论（做多/做空/观望），再给数据支撑
- 每句话都要有数字（价格、百分比、指标值）
- 用中文回答
- 禁止使用任何称呼（兄弟、老铁、朋友等），直接给结论
- 语气专业克制，不用口语化表达
- 绝不暴露内部系统信息
- 如果用户只是打招呼，简短回应并主动问"需要我分析哪个币？"
"""

SYSTEM_PROMPT_SENTIMENT = """你是加密货币市场情绪分析师。

你的职责：
1. 分析给定币种的市场情绪
2. 综合考虑技术面数据和市场热度
3. 给出情绪评分（-1.0 极度恐慌 到 +1.0 极度贪婪）

输出格式（严格 JSON）：
{
    "score": 0.3,
    "label": "轻度贪婪",
    "confidence": 0.7,
    "summary": "一句话总结",
    "key_factors": ["因素1", "因素2"]
}"""


# ============================================================
# 内部工具函数
# ============================================================

def _call_llm(messages: list, provider: str = None, model_type: str = "default", max_tokens: int = None) -> str:
    """
    统一的大模型调用方法。
    支持通过 provider 参数切换不同的模型提供商。
    """
    if provider is None:
        provider = ACTIVE_PROVIDER

    config = AI_PROVIDERS.get(provider)
    if not config:
        return f"[错误] 不支持的 AI 提供商: {provider}"

    if not config["api_key"]:
        return f"[错误] {config['name']} API Key 未配置"

    model = config["models"].get(model_type, config["models"]["default"])
    timeout = config["timeout"]
    tokens = max_tokens or config["max_tokens"]

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": tokens,
        "temperature": 0.7,
    }).encode()

    req = urllib.request.Request(
        config["base_url"],
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config['api_key']}",
            "User-Agent": "H_V3/3.0.0",
        },
    )

    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.readable() else ""
        return f"[API错误] HTTP {e.code}: {body[:200]}"
    except urllib.error.URLError as e:
        return f"[网络错误] {e.reason}"
    except Exception as e:
        return f"[异常] {str(e)}"


# ============================================================
# MCP Tools 定义
# ============================================================

@mcp.tool()
def chat(query: str, engine_data: dict = None, provider: str = None) -> dict:
    """
    AI 对话：基于引擎数据上下文回答用户的交易相关问题。

    Args:
        query: 用户的问题
        engine_data: 引擎提供的技术指标数据（可选，来自 scan_symbol 的输出）
        provider: 指定 AI 提供商（grok/deepseek/openai），不传则使用默认

    Returns:
        - response: AI 的回答文本
        - model: 使用的模型名称
        - provider: 使用的提供商
    """
    # 构建上下文
    context_text = ""
    if engine_data:
        context_text = f"""

【当前引擎数据】
- 币种: {engine_data.get('symbol', '未知')} ({engine_data.get('name', '')})
- 当前价: {engine_data.get('entry_price', 0):,.2f} USDT
- 方向信号: {engine_data.get('direction', 'neutral')}
- 综合评分: {engine_data.get('score', 0)}/5
- 赫斯特指数: {engine_data.get('hurst', 0):.4f} ({engine_data.get('market_state', '未知')})
- RSI: {engine_data.get('rsi', 0):.1f}
- EMA: fast={engine_data.get('ema_fast', 0):.2f} slow={engine_data.get('ema_slow', 0):.2f}
- MACD柱: {engine_data.get('macd_histogram', 0):.4f}
- ATR: {engine_data.get('atr', 0):.2f}
- 止盈: {engine_data.get('tp_price', 0):,.2f}
- 止损: {engine_data.get('sl_price', 0):,.2f}
- 风险等级: {engine_data.get('risk_level', '未知')}
- 信号理由: {engine_data.get('reason', '')}
"""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_TRADER + context_text},
        {"role": "user", "content": query},
    ]

    use_provider = provider or ACTIVE_PROVIDER
    response = _call_llm(messages, provider=use_provider, model_type="fast")
    model_name = AI_PROVIDERS[use_provider]["models"]["fast"]

    return {
        "response": response,
        "model": model_name,
        "provider": AI_PROVIDERS[use_provider]["name"],
        "has_engine_data": engine_data is not None,
    }


@mcp.tool()
def analyze_sentiment(symbol: str, market_context: str = "", provider: str = None) -> dict:
    """
    分析指定币种的市场情绪。

    Args:
        symbol: 币种符号（BTC/ETH/SOL 等）
        market_context: 额外的市场上下文信息（如新闻摘要）
        provider: 指定 AI 提供商

    Returns:
        - score: 情绪评分 (-1.0 到 +1.0)
        - label: 情绪标签
        - confidence: 置信度
        - summary: 一句话总结
        - key_factors: 关键影响因素
    """
    user_prompt = f"请分析 {symbol} 当前的市场情绪。"
    if market_context:
        user_prompt += f"\n\n额外市场信息：{market_context}"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_SENTIMENT},
        {"role": "user", "content": user_prompt},
    ]

    use_provider = provider or ACTIVE_PROVIDER
    response = _call_llm(messages, provider=use_provider, model_type="fast")

    # 尝试解析 JSON 响应
    try:
        # 提取 JSON 部分
        start = response.find("{")
        end = response.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(response[start:end])
            return {
                "error": False,
                "symbol": symbol,
                "score": float(parsed.get("score", 0)),
                "label": parsed.get("label", "中性"),
                "confidence": float(parsed.get("confidence", 0.5)),
                "summary": parsed.get("summary", ""),
                "key_factors": parsed.get("key_factors", []),
                "provider": AI_PROVIDERS[use_provider]["name"],
            }
    except (json.JSONDecodeError, ValueError):
        pass

    # 解析失败，返回原始文本
    return {
        "error": False,
        "symbol": symbol,
        "score": 0,
        "label": "解析失败",
        "confidence": 0,
        "summary": response[:200],
        "key_factors": [],
        "provider": AI_PROVIDERS[use_provider]["name"],
        "raw_response": response,
    }


@mcp.tool()
def summarize_market(symbols_data: list[dict], provider: str = None) -> dict:
    """
    基于多个币种的引擎扫描数据，生成市场综合总结。

    Args:
        symbols_data: 多个币种的 scan_symbol 结果数组
        provider: 指定 AI 提供商

    Returns:
        - summary: 市场综合总结文本
        - top_opportunity: 最佳机会币种
        - market_mood: 整体市场情绪
    """
    # 构建数据摘要
    data_text = "以下是当前各币种的引擎扫描数据：\n\n"
    for d in symbols_data:
        if d.get("error"):
            continue
        data_text += f"【{d.get('symbol', '?')}】"
        data_text += f" 方向={d.get('direction')} 评分={d.get('score')}"
        data_text += f" H={d.get('hurst', 0):.3f}({d.get('market_state')})"
        data_text += f" RSI={d.get('rsi', 0):.0f}"
        data_text += f" 价格={d.get('entry_price', 0):,.2f}\n"

    messages = [
        {"role": "system", "content": "你是加密货币市场分析师。请基于以下多币种数据，给出简洁的市场总结（100字以内），指出最佳交易机会和整体市场情绪。用中文回答。"},
        {"role": "user", "content": data_text},
    ]

    use_provider = provider or ACTIVE_PROVIDER
    response = _call_llm(messages, provider=use_provider, model_type="fast")

    # 找出最佳机会
    best = None
    best_score = 0
    for d in symbols_data:
        if d.get("error"):
            continue
        score = abs(d.get("score", 0))
        if score > best_score:
            best_score = score
            best = d.get("symbol")

    return {
        "summary": response,
        "top_opportunity": best,
        "symbols_count": len(symbols_data),
        "provider": AI_PROVIDERS[use_provider]["name"],
    }


@mcp.tool()
def switch_provider(provider: str) -> dict:
    """
    切换当前激活的 AI 模型提供商。

    Args:
        provider: 目标提供商名称（grok/deepseek/openai）

    Returns:
        切换结果确认
    """
    global ACTIVE_PROVIDER

    if provider not in AI_PROVIDERS:
        return {
            "error": True,
            "message": f"不支持的提供商: {provider}",
            "available": list(AI_PROVIDERS.keys()),
        }

    config = AI_PROVIDERS[provider]
    if not config["api_key"]:
        return {
            "error": True,
            "message": f"{config['name']} 的 API Key 未配置，无法切换",
        }

    old_provider = ACTIVE_PROVIDER
    ACTIVE_PROVIDER = provider

    return {
        "error": False,
        "message": f"已从 {AI_PROVIDERS[old_provider]['name']} 切换到 {config['name']}",
        "active_provider": provider,
        "active_model": config["models"]["default"],
    }


@mcp.tool()
def list_providers() -> dict:
    """
    列出所有支持的 AI 模型提供商及其状态。

    Returns:
        所有提供商的配置信息和可用状态
    """
    result = {}
    for key, config in AI_PROVIDERS.items():
        result[key] = {
            "name": config["name"],
            "available": bool(config["api_key"]),
            "models": config["models"],
            "is_active": key == ACTIVE_PROVIDER,
        }
    return result


# ============================================================
# MCP Resources
# ============================================================

@mcp.resource("config://ai_providers")
def get_ai_config() -> str:
    """返回当前 AI 提供商配置（隐藏 API Key）"""
    safe_config = {}
    for key, config in AI_PROVIDERS.items():
        safe_config[key] = {
            "name": config["name"],
            "available": bool(config["api_key"]),
            "models": config["models"],
            "is_active": key == ACTIVE_PROVIDER,
        }
    return json.dumps(safe_config, indent=2)


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
