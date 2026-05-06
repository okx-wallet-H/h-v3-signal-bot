"""
H V3 Bot 推送层 (Bot Layer) v3.5
================================
四层架构第四层：Telegram Bot 信号推送

功能：
1. 固定时间推送（北京时间 0/4/8/12/16/20 点）
2. /signal 命令手动获取信号
3. /status 查看系统状态
4. AI分析（Grok）- 自然语言问答
5. 信号附带回测绩效数据
6. 群组模式：只有@Bot才回复
"""

import os
import sys
import json
import time
import asyncio
import logging
import traceback
import httpx
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, List

# 路径设置
sys.path.insert(0, '/root/h_v3')

import h_v3_data_api as data_api
import h_v3_strategy as strategy
import h_v3_backtest as backtest

from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ============================================================
# 配置
# ============================================================

BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
BOT_USERNAME = "H_NO_1_bot"
CHAT_IDS = []
CHAT_IDS_FILE = "/root/h_v3/chat_ids.json"

# 北京时间推送时间（UTC+8）
PUSH_HOURS_BJT = [0, 4, 8, 12, 16, 20]

# Grok AI 配置
GROK_API_KEY = "os.environ.get("GROK_API_KEY", "")"
GROK_BASE_URL = "https://api.x.ai/v1"
GROK_MODEL = "grok-3-mini-fast"

AI_SYSTEM_PROMPT = """你是专业的加密货币合约交易分析师。基于提供的多维度实时数据，给出简洁明确的交易建议。

规则：
- 直接给结论：做多/做空/观望
- 用1-3句大白话解释为什么
- 如果数据有矛盾，指出关键矛盾点和你倾向的方向
- 给出具体的止损价位
- 不要废话、不要客套、不要称呼用户
- 说人话，别用专业术语堆砌

重要分析逻辑：
- 空头入场价 > 现价 = 空头浮盈（价格跌了他们赚了）
- 空头入场价 < 现价 = 空头浮亏（被套了，可能轧空）
- 多头入场价 < 现价 = 多头浮盈
- 多头入场价 > 现价 = 多头浮亏（被套了）
- 聪明钱方向和技术面矛盾时要特别小心
- 被套的一方如果止损会加速反方向运动"""

# 日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [Bot] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('/root/h_v3/bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("h_v3_bot")


# ============================================================
# 群组过滤
# ============================================================

def _is_group_mention(update: Update) -> bool:
    """群组中只有@Bot或回复Bot消息才响应，私聊始终响应"""
    chat_type = update.effective_chat.type
    if chat_type in ("group", "supergroup"):
        msg = update.message
        if msg and msg.text:
            # 检查是否@了Bot
            if f"@{BOT_USERNAME}" in msg.text:
                return True
            # 检查命令是否带Bot用户名（如 /signal@H_NO_1_bot）
            if msg.text.startswith("/") and f"@{BOT_USERNAME}" in msg.text.split()[0]:
                return True
            # 检查是否回复Bot的消息
            if msg.reply_to_message and msg.reply_to_message.from_user:
                if msg.reply_to_message.from_user.username == BOT_USERNAME:
                    return True
            return False
        return False
    return True  # 私聊始终响应


# ============================================================
# Grok AI 分析
# ============================================================

async def ai_analyze(symbol: str, data: dict, signal) -> str:
    """调用Grok AI分析市场数据，返回人话结论"""
    try:
        # 构建数据摘要给AI
        price = data.get("price", 0)
        rsi = data.get("rsi", 0)
        ema_5 = data.get("ema_5", 0)
        ema_20 = data.get("ema_20", 0)
        supertrend_dir = data.get("supertrend_dir", "")
        funding_rate = data.get("funding_rate", 0)
        long_ratio = data.get("long_short_ratio", 0)
        
        # 聪明钱数据
        sm_long = data.get("smart_money_long_pct", 0)
        sm_weighted = data.get("smart_money_weighted_long", 0)
        sm_long_entry = data.get("smart_money_long_entry", 0)
        sm_short_entry = data.get("smart_money_short_entry", 0)
        sm_net = data.get("smart_money_net_notional", 0)
        sm_vs24h = data.get("smart_money_vs24h", 0)
        sm_vs7d = data.get("smart_money_vs7d", 0)
        sm_traders = data.get("smart_money_traders_count", 0)
        sm_long_wr = data.get("smart_money_long_wr", 0)
        sm_short_wr = data.get("smart_money_short_wr", 0)
        
        # 精英和巨鲸
        sm_elite_long = data.get("smart_money_elite_long_pct", 0)
        sm_whale_long = data.get("smart_money_whale_long_pct", 0)

        # 多空浮盈浮亏判断
        long_pnl = ""
        short_pnl = ""
        if sm_long_entry and price:
            if price > sm_long_entry:
                long_pnl = f"多头浮盈{(price-sm_long_entry)/sm_long_entry*100:.1f}%"
            else:
                long_pnl = f"多头浮亏{(sm_long_entry-price)/sm_long_entry*100:.1f}%"
        if sm_short_entry and price:
            if price < sm_short_entry:
                short_pnl = f"空头浮盈{(sm_short_entry-price)/sm_short_entry*100:.1f}%"
            else:
                short_pnl = f"空头浮亏{(price-sm_short_entry)/sm_short_entry*100:.1f}%"

        data_prompt = f"""
{symbol}/USDT 永续合约 实时数据：

【价格与技术面】
- 现价: ${price:,.1f}
- RSI(14): {rsi:.1f}（>70超买, <30超卖）
- EMA5: ${ema_5:,.1f} / EMA20: ${ema_20:,.1f}（{'多头排列' if ema_5 > ema_20 else '空头排列'}）
- SuperTrend: {'看涨' if supertrend_dir == 'buy' else '看跌'}
- 资金费率: {funding_rate*100:.4f}%（正=多头付费，负=空头付费）
- 散户多空比: {long_ratio:.0%}多/{1-long_ratio:.0%}空

【聪明钱持仓（1000+顶级交易员）】
- 方向: {sm_long:.0%}做多 / {1-sm_long:.0%}做空（共{sm_traders}人）
- 精英交易员（高胜率+高盈利）: {sm_elite_long:.0%}做多
- 巨鲸（大资金）: {sm_whale_long:.0%}做多
- 多方平均胜率: {sm_long_wr:.0%} | 空方平均胜率: {sm_short_wr:.0%}
- 多头平均入场价: ${sm_long_entry:,.0f}（{long_pnl}）
- 空头平均入场价: ${sm_short_entry:,.0f}（{short_pnl}）
- 净持仓资金: ${sm_net/1e6:+.1f}M（正=多头主导，负=空头主导）
- 24h变化: {sm_vs24h:+.2f} | 7天变化: {sm_vs7d:+.2f}（正=转多，负=转空）

【策略引擎8因子评分】
- 总分: {signal.strength:+.2f}（>3.5强信号，<-3.5强空信号）
- 方向: {signal.direction}
- 各因子: {json.dumps(signal.factor_scores, ensure_ascii=False) if signal.factor_scores else 'N/A'}

请基于以上数据给出交易建议。
"""

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{GROK_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROK_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": GROK_MODEL,
                    "messages": [
                        {"role": "system", "content": AI_SYSTEM_PROMPT},
                        {"role": "user", "content": data_prompt}
                    ],
                    "max_tokens": 300,
                    "temperature": 0.3
                }
            )
            
            if resp.status_code == 200:
                result = resp.json()
                return result["choices"][0]["message"]["content"].strip()
            else:
                logger.error(f"Grok API error: {resp.status_code} {resp.text[:200]}")
                return ""
    except Exception as e:
        logger.error(f"AI analyze error: {e}")
        return ""


# ============================================================
# 格式化输出
# ============================================================

def format_signal_message(signal: strategy.Signal, bt_result=None, ai_conclusion: str = "") -> str:
    """格式化信号推送消息 - 人话版"""
    symbol = signal.symbol
    direction = signal.direction
    strength = signal.strength
    confidence = signal.confidence

    # 方向中文
    dir_map = {"long": "📈 做多", "short": "📉 做空", "neutral": "⏸ 观望"}
    dir_text = dir_map.get(direction, "⏸ 观望")

    # 星级
    stars = "⭐️" * confidence if confidence > 0 and direction != "neutral" else ""

    lines = []
    lines.append(f"*{symbol}/USDT 永续*")
    lines.append("─" * 20)
    lines.append(f"方向: {dir_text} {stars}")
    lines.append(f"强度: {strength:+.2f}")

    # 聪明钱矛盾警告
    if signal.summary and signal.summary.get("sm_conflict"):
        lines.append(f"⚠️ *聪明钱反向警告* - 止损已收紧")

    # 价格信息
    if signal.entry_price:
        lines.append(f"价格: ${signal.entry_price:,.1f}")

    if direction != "neutral":
        if signal.stop_loss:
            lines.append(f"止损: ${signal.stop_loss:,.1f}")
        if signal.take_profit_1:
            lines.append(f"止盈1: ${signal.take_profit_1:,.1f}")
        if signal.take_profit_2:
            lines.append(f"止盈2: ${signal.take_profit_2:,.1f}")
        lines.append(f"杠杆: {signal.leverage_suggest}x")

    # 因子得分
    lines.append("")
    lines.append("─" * 20)
    lines.append("*因子明细*")
    if signal.factor_scores:
        for name, score in signal.factor_scores.items():
            name_cn = {
                "trend": "趋势",
                "momentum": "动量",
                "macd": "MACD",
                "bollinger": "布林",
                "money_flow": "资金流",
                "market_structure": "市场结构",
                "smart_money": "聪明钱",
                "multi_tf": "多TF",
            }.get(name, name)
            indicator = "🟢" if score > 0.3 else "🔴" if score < -0.3 else "⚪"
            lines.append(f"  {indicator} {name_cn}: {score:+.2f}")

    # 聪明钱 - 人话版
    if signal.summary:
        s = signal.summary
        lines.append("")
        lines.append("─" * 20)
        lines.append("*聪明钱*（1000+顶级交易员持仓）")
        
        # 方向判断
        sm_long = s.get("sm_all_long", 0.5)
        if sm_long is not None:
            sm_short = 1 - sm_long
            if sm_long > 0.6:
                lines.append(f"  方向: {sm_long:.0%}做多 📈 大多数大佬看涨")
            elif sm_long < 0.4:
                lines.append(f"  方向: {sm_short:.0%}做空 📉 大多数大佬看跌")
            else:
                lines.append(f"  方向: 多空各半 ⚖️ 大佬们意见分歧")

        # 精英和巨鲸
        elite = s.get("sm_elite_long")
        whale = s.get("sm_whale_long")
        if elite is not None and whale is not None:
            lines.append(f"  精英: {elite:.0%}多 | 巨鲸: {whale:.0%}多")
        
        # 浮盈浮亏 - 关键信息
        price = s.get("price") or (signal.entry_price if signal.entry_price else 0)
        long_entry = s.get("sm_long_entry", 0)
        short_entry = s.get("sm_short_entry", 0)
        
        if long_entry and price and long_entry > 0:
            if price > long_entry:
                lines.append(f"  多头入场${long_entry:,.0f}（浮盈✅）")
            else:
                lines.append(f"  多头入场${long_entry:,.0f}（被套❌）")
        
        if short_entry and price and short_entry > 0:
            if price < short_entry:
                lines.append(f"  空头入场${short_entry:,.0f}（浮盈✅）")
            else:
                lines.append(f"  空头入场${short_entry:,.0f}（被套❌ 可能轧空）")

        # 趋势变化 - 人话
        vs24h = s.get("sm_vs24h")
        vs7d = s.get("sm_vs7d")
        if vs7d is not None and abs(vs7d) > 0.1:
            if vs7d > 0:
                lines.append(f"  趋势: 7天内持续转多（+{vs7d:.0%}）")
            else:
                lines.append(f"  趋势: 7天内持续转空（{vs7d:.0%}）")
        elif vs24h is not None and abs(vs24h) > 0.05:
            if vs24h > 0:
                lines.append(f"  趋势: 24h内转多（+{vs24h:.0%}）")
            else:
                lines.append(f"  趋势: 24h内转空（{vs24h:.0%}）")

        # 净资金
        net = s.get("sm_net_notional")
        if net is not None:
            if abs(net) >= 1e6:
                net_str = f"${abs(net)/1e6:.1f}M"
            else:
                net_str = f"${abs(net)/1e3:.0f}K"
            if net > 0:
                lines.append(f"  资金: 多头净持仓{net_str}")
            else:
                lines.append(f"  资金: 空头净持仓{net_str}")

    # 关键技术指标
    if signal.summary:
        s = signal.summary
        lines.append("")
        lines.append("─" * 20)
        lines.append("*技术面*")
        if s.get("rsi"):
            rsi = s["rsi"]
            rsi_label = "超买⚠️" if rsi > 70 else "超卖⚠️" if rsi < 30 else "正常"
            lines.append(f"  RSI: {rsi:.1f} ({rsi_label})")
        if s.get("ema_5") and s.get("ema_20"):
            ema_rel = "多头排列📈" if s['ema_5'] > s['ema_20'] else "空头排列📉"
            lines.append(f"  EMA: {ema_rel}")
        if s.get("supertrend"):
            st = "看涨" if s['supertrend'] == 'buy' else "看跌"
            lines.append(f"  SuperTrend: {st}")
        if s.get("funding_rate") is not None:
            fr = s["funding_rate"]
            fr_label = "多头付费" if fr > 0 else "空头付费"
            lines.append(f"  资金费率: {fr*100:.4f}% ({fr_label})")

    # 回测绩效
    if bt_result:
        lines.append("")
        lines.append("─" * 20)
        lines.append("*历史绩效*")
        lines.append(f"  胜率{bt_result.win_rate*100:.0f}% | 盈亏比{bt_result.profit_factor:.1f} | 回撤{bt_result.max_drawdown_pct:.1f}%")
        lines.append(f"  ({bt_result.period_days}天/{bt_result.total_trades}笔)")

    # AI结论
    if ai_conclusion:
        lines.append("")
        lines.append("─" * 20)
        lines.append("*AI分析*")
        lines.append(f"  {ai_conclusion}")

    # 时间戳
    bjt = datetime.now(timezone(timedelta(hours=8)))
    lines.append("")
    lines.append(f"_{bjt.strftime('%m/%d %H:%M')} BJT | v3.5_")

    return "\n".join(lines)


def format_status_message() -> str:
    """格式化系统状态消息"""
    status = data_api.get_status()
    lines = []
    lines.append("*系统状态*")
    lines.append("─" * 20)
    lines.append(f"数据层: {'🟢 运行中' if status.get('running') else '🔴 停止'}")
    lines.append(f"监控币种: {', '.join(status.get('symbols', []))}")

    if status.get("cache_ages"):
        lines.append("")
        lines.append("*缓存状态*")
        for sym, age in status["cache_ages"].items():
            fresh = "🟢" if age < 300 else "🟡" if age < 600 else "🔴"
            lines.append(f"  {fresh} {sym}: {age}s ago")

    lines.append("")
    lines.append("*架构*")
    lines.append("  L1: 数据接口层 (OKX CLI + REST API)")
    lines.append("  L2: 策略引擎 (8因子加权)")
    lines.append("  L3: 回测验证 (100天历史)")
    lines.append("  L4: Bot推送 + Grok AI")

    return "\n".join(lines)


# ============================================================
# Bot命令处理
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    if not _is_group_mention(update):
        return
    chat_id = update.effective_chat.id
    _save_chat_id(chat_id)
    await update.message.reply_text(
        "*H V3 AI合约策略信号系统*\n"
        "─" * 20 + "\n"
        "四层架构 + Grok AI 已上线\n\n"
        "命令:\n"
        "  /signal [币种] - 获取信号+AI分析\n"
        "  /all - 全部币种信号\n"
        "  /status - 系统状态\n"
        "  /backtest [币种] - 回测\n\n"
        "直接发消息也可以，比如:\n"
        "  「BTC怎么看」「ETH能做空吗」\n\n"
        f"推送时间: BJT {'/'.join(str(h) for h in PUSH_HOURS_BJT)}点\n"
        f"群组中请@{BOT_USERNAME}",
        parse_mode="Markdown"
    )


async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /signal 命令"""
    if not _is_group_mention(update):
        return
    chat_id = update.effective_chat.id
    _save_chat_id(chat_id)

    # 解析币种
    args = context.args
    # 去掉可能的@Bot后缀
    symbol = args[0].upper().replace(f"@{BOT_USERNAME}", "") if args else "BTC"
    # 清理非字母字符
    symbol = ''.join(c for c in symbol if c.isalpha())
    if not symbol:
        symbol = "BTC"

    await update.message.reply_text(f"正在分析 {symbol}...")

    try:
        # 从数据接口层获取数据
        data = data_api.get_data(symbol)
        if not data:
            data = data_api.force_refresh(symbol)

        if not data:
            await update.message.reply_text(f"⚠️ {symbol} 数据不可用，请稍后重试")
            return

        # 策略引擎分析
        signal = strategy.analyze(symbol, data)

        # 获取回测绩效
        bt = backtest.get_backtester()
        bt_result = bt._cache.get(symbol) or bt._cache.get(f"{symbol}_quick")

        # AI分析
        ai_conclusion = await ai_analyze(symbol, data, signal)

        # 格式化并发送
        msg = format_signal_message(signal, bt_result, ai_conclusion)
        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Signal error: {e}\n{traceback.format_exc()}")
        await update.message.reply_text(f"⚠️ 分析出错: {str(e)[:100]}")


async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /all 命令 - 全部币种信号"""
    if not _is_group_mention(update):
        return
    chat_id = update.effective_chat.id
    _save_chat_id(chat_id)

    await update.message.reply_text("正在分析全部币种...")

    for symbol in data_api.SYMBOLS:
        try:
            data = data_api.get_data(symbol)
            if not data:
                continue
            signal = strategy.analyze(symbol, data)
            bt = backtest.get_backtester()
            bt_result = bt._cache.get(symbol) or bt._cache.get(f"{symbol}_quick")
            # /all 不调AI（太慢），只给数据
            msg = format_signal_message(signal, bt_result)
            await update.message.reply_text(msg, parse_mode="Markdown")
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"All signal error for {symbol}: {e}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /status 命令"""
    if not _is_group_mention(update):
        return
    _save_chat_id(update.effective_chat.id)
    msg = format_status_message()
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /backtest 命令"""
    if not _is_group_mention(update):
        return
    _save_chat_id(update.effective_chat.id)
    args = context.args
    symbol = args[0].upper() if args else "BTC"

    await update.message.reply_text(f"正在回测 {symbol}...")

    try:
        result = backtest.run_quick(symbol)
        if result:
            lines = [
                f"*{symbol} 回测结果*",
                "─" * 20,
                f"周期: {result.period_days}天",
                f"交易数: {result.total_trades}",
                f"胜率: {result.win_rate*100:.1f}%",
                f"盈亏比: {result.profit_factor:.2f}",
                f"平均盈利: {result.avg_win_pct:.2f}%",
                f"最大回撤: {result.max_drawdown_pct:.1f}%",
                f"Sharpe: {result.sharpe_ratio:.2f}",
                f"总收益: {result.total_return_pct:.2f}%",
                "",
                f"多头: {result.long_trades}笔 胜率{result.long_win_rate*100:.0f}%",
                f"空头: {result.short_trades}笔 胜率{result.short_win_rate*100:.0f}%",
            ]
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ 回测失败，数据不足")
    except Exception as e:
        logger.error(f"Backtest error: {e}")
        await update.message.reply_text(f"⚠️ 回测出错: {str(e)[:100]}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理普通消息 - AI自然语言分析"""
    if not _is_group_mention(update):
        return
    
    chat_id = update.effective_chat.id
    _save_chat_id(chat_id)
    text = update.message.text.strip()
    
    # 去掉@Bot部分
    text = text.replace(f"@{BOT_USERNAME}", "").strip()
    
    if not text:
        return

    # 识别币种
    symbol = None
    for s in data_api.SYMBOLS:
        if s.lower() in text.lower():
            symbol = s
            break
    if not symbol:
        symbol = "BTC"

    await update.message.reply_text(f"正在分析 {symbol}...")

    try:
        # 获取数据和信号
        data = data_api.get_data(symbol)
        if not data:
            await update.message.reply_text(f"数据加载中，请稍后使用 /signal {symbol}")
            return

        signal = strategy.analyze(symbol, data)
        
        # AI分析
        ai_conclusion = await ai_analyze(symbol, data, signal)
        
        # 获取回测
        bt = backtest.get_backtester()
        bt_result = bt._cache.get(symbol) or bt._cache.get(f"{symbol}_quick")
        
        msg = format_signal_message(signal, bt_result, ai_conclusion)
        await update.message.reply_text(msg, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Message handler error: {e}\n{traceback.format_exc()}")
        await update.message.reply_text(f"⚠️ 分析出错: {str(e)[:100]}")


# ============================================================
# 定时推送
# ============================================================

async def scheduled_push(context: ContextTypes.DEFAULT_TYPE):
    """定时推送信号"""
    bjt_now = datetime.now(timezone(timedelta(hours=8)))
    current_hour = bjt_now.hour

    if current_hour not in PUSH_HOURS_BJT:
        return

    logger.info(f"Scheduled push at BJT {current_hour}:00")

    chat_ids = _load_chat_ids()
    if not chat_ids:
        logger.warning("No chat_ids for push")
        return

    for symbol in data_api.SYMBOLS:
        try:
            data = data_api.get_data(symbol)
            if not data:
                continue

            signal = strategy.analyze(symbol, data)

            # 只推送有方向的信号（非观望）或BTC/ETH（始终推送）
            if signal.direction == "neutral" and symbol not in ["BTC", "ETH"]:
                continue

            bt = backtest.get_backtester()
            bt_result = bt._cache.get(symbol) or bt._cache.get(f"{symbol}_quick")

            # 定时推送也带AI分析（只给BTC和ETH）
            ai_conclusion = ""
            if symbol in ["BTC", "ETH"]:
                ai_conclusion = await ai_analyze(symbol, data, signal)

            msg = format_signal_message(signal, bt_result, ai_conclusion)

            for chat_id in chat_ids:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, text=msg, parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Push to {chat_id} failed: {e}")

            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Push error for {symbol}: {e}")


# ============================================================
# 工具函数
# ============================================================

def _save_chat_id(chat_id: int):
    """保存chat_id"""
    global CHAT_IDS
    if chat_id not in CHAT_IDS:
        CHAT_IDS.append(chat_id)
        try:
            with open(CHAT_IDS_FILE, 'w') as f:
                json.dump(CHAT_IDS, f)
        except Exception:
            pass


def _load_chat_ids() -> List[int]:
    """加载chat_ids"""
    global CHAT_IDS
    try:
        if os.path.exists(CHAT_IDS_FILE):
            with open(CHAT_IDS_FILE, 'r') as f:
                CHAT_IDS = json.load(f)
    except Exception:
        pass
    return CHAT_IDS


# ============================================================
# 主入口
# ============================================================

def main():
    """启动Bot"""
    logger.info("=" * 50)
    logger.info("H V3 AI合约策略信号系统 v3.5 启动")
    logger.info("四层架构 + Grok AI + 群组模式")
    logger.info("=" * 50)

    # 加载chat_ids
    _load_chat_ids()

    # 初始化数据接口层（启动后台缓存）
    logger.info("初始化数据接口层...")
    data_api.init()

    # 初始化回测层
    logger.info("初始化回测层...")
    backtest.get_backtester()

    # 等待首次数据加载
    logger.info("等待首次数据加载...")
    time.sleep(5)

    # 创建Bot
    app = Application.builder().token(BOT_TOKEN).build()

    # 注册命令
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 定时任务：每小时检查是否需要推送
    job_queue = app.job_queue
    # DISABLED: job_queue.run_repeating(scheduled_push, interval=3600, first=60)

    # 启动时运行一次快速回测
    async def init_backtest(context):
        logger.info("Running initial quick backtest...")
        for symbol in ["BTC", "ETH"]:
            try:
                backtest.run_quick(symbol)
                logger.info(f"Quick backtest done for {symbol}")
            except Exception as e:
                logger.error(f"Init backtest error: {e}")

    job_queue.run_once(init_backtest, when=30)

    logger.info("Bot started, polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
