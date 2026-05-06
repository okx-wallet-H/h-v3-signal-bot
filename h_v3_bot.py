"""
H_V3 Bot - 主调度中心
========================
作为 MCP Client，连接所有 MCP Server（OKX Market / Engine / AI），
并通过 Telegram Bot 与用户交互。

架构角色：纯编排层，不包含任何业务逻辑计算。
所有能力通过调用 MCP Server 的 Tools 实现。

特性：
  - 进程锁：防止多实例运行导致 409 冲突
  - 命令路由：解析 Telegram 命令并分发到对应 MCP Server
  - AI 对话：自动检测币种 → 调引擎 → 喂 AI → 加水印 → 回复
  - 引用回复：回复时引用提问者的原始消息
  - 即时反馈：收到消息后立即回复"AI Agent 正在分析"
  - 定时扫描：每 4 小时自动扫描并推送信号
  - 优雅退出：收到 SIGTERM 时正确清理资源
"""

import os
import sys
import json
import time
import signal
import fcntl
import threading
import traceback
import urllib.request
import urllib.error
from datetime import datetime

# ============================================================
# 进程锁（彻底防止多实例）
# ============================================================

PID_FILE = "/tmp/h_v3_bot.pid"


def acquire_lock():
    """获取进程锁，如果已有实例运行则退出"""
    try:
        fp = open(PID_FILE, "w")
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fp.write(str(os.getpid()))
        fp.flush()
        return fp  # 必须保持文件句柄打开
    except IOError:
        print("[致命] 检测到另一个 H_V3 Bot 实例正在运行，退出。")
        sys.exit(1)


# ============================================================
# 配置
# ============================================================

# Telegram
TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = -5164059069  # ai策略群
TELEGRAM_BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Bot 用户名（用于检测群聊中的 @ 提及）
BOT_USERNAME = "H_NO_1_bot"

# 定时扫描间隔（秒）
# 定时推送时间点（北京时间 UTC+8）
SCAN_HOURS = [0, 4, 8, 12, 16, 20]  # 对齐4H K线周期

# 支持的币种
SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "OKB"]

# 币种别名映射（用于 AI 对话中检测用户提到的币种）
SYMBOL_ALIASES = {
    "BTC": ["btc", "比特币", "bitcoin", "大饼"],
    "ETH": ["eth", "以太坊", "ethereum", "以太", "姨太"],
    "SOL": ["sol", "solana", "索拉纳"],
    "DOGE": ["doge", "狗狗币", "dogecoin", "狗币"],
    "OKB": ["okb"],
}


# ============================================================
# 导入 MCP Server 模块（同进程直接调用）
# ============================================================

# 注意：在生产环境中，这些可以改为通过 MCP stdio/SSE 协议远程调用
# 当前为简化部署，采用同进程直接导入的方式
from h_v3_mcp_okx_market import get_ticker, get_tickers
from h_v3_mcp_engine import scan_symbol
from h_v3_mcp_ai import chat, analyze_sentiment, summarize_market, list_providers
from h_v3_mcp_smartmoney import start_smart_money_service

# 启动聪明钱后台缓存服务
start_smart_money_service()


# ============================================================
# Telegram 通信层
# ============================================================

class TelegramClient:
    """Telegram Bot API 通信客户端"""

    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.offset = 0

    def send_message(self, text: str, chat_id: int = None, parse_mode: str = "Markdown",
                     reply_to_message_id: int = None) -> bool:
        """发送消息，支持引用回复"""
        target = chat_id or TELEGRAM_CHAT_ID
        payload = {
            "chat_id": target,
            "text": text,
            "parse_mode": parse_mode,
        }

        # 引用回复
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.base_url}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "H_V3/3.0.0"},
        )

        try:
            urllib.request.urlopen(req, timeout=10)
            return True
        except Exception as e:
            print(f"[推送失败] {e}")
            # 如果 Markdown 解析失败，尝试纯文本重发
            if "400" in str(e):
                payload["parse_mode"] = ""
                data2 = json.dumps(payload).encode()
                req2 = urllib.request.Request(
                    f"{self.base_url}/sendMessage",
                    data=data2,
                    headers={"Content-Type": "application/json", "User-Agent": "H_V3/3.0.0"},
                )
                try:
                    urllib.request.urlopen(req2, timeout=10)
                    return True
                except Exception as e2:
                    print(f"[纯文本重发也失败] {e2}")
            return False

    def get_updates(self) -> list:
        """获取新消息（long polling）"""
        url = f"{self.base_url}/getUpdates?offset={self.offset}&timeout=30"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "H_V3/3.0.0"})
            resp = urllib.request.urlopen(req, timeout=35)
            data = json.loads(resp.read())
            if data.get("ok"):
                return data.get("result", [])
        except Exception as e:
            if "409" not in str(e):
                print(f"[getUpdates] {e}")
            time.sleep(5)
        return []


# ============================================================
# 命令处理器
# ============================================================

class CommandRouter:
    """命令路由器：解析命令并调用对应的 MCP Server Tools"""

    def __init__(self, telegram: TelegramClient):
        self.tg = telegram
        self.commands = {
            "/start": self.cmd_start,
            "/help": self.cmd_help,
            "/scan": self.cmd_scan,
            "/signal": self.cmd_signal,
            "/btc": lambda cid, mid: self.cmd_symbol("BTC", cid, mid),
            "/eth": lambda cid, mid: self.cmd_symbol("ETH", cid, mid),
            "/sol": lambda cid, mid: self.cmd_symbol("SOL", cid, mid),
            "/doge": lambda cid, mid: self.cmd_symbol("DOGE", cid, mid),
            "/okb": lambda cid, mid: self.cmd_symbol("OKB", cid, mid),
            "/sentiment": self.cmd_sentiment,
            "/status": self.cmd_status,
            "/version": self.cmd_version,
            "/providers": self.cmd_providers,
        }

    def route(self, command: str, chat_id: int, text: str = "", message_id: int = None):
        """路由命令到对应处理器"""
        cmd = command.split("@")[0].lower()  # 去掉 @botname
        handler = self.commands.get(cmd)
        if handler:
            try:
                handler(chat_id, message_id)
            except Exception as e:
                self.tg.send_message(f"命令执行出错: {e}", chat_id, reply_to_message_id=message_id)
                print(f"[命令错误] {cmd}: {e}")
                traceback.print_exc()
        else:
            # 非命令消息，走 AI 对话
            self.handle_ai_chat(text, chat_id, message_id)

    def cmd_start(self, chat_id: int, message_id: int = None):
        msg = """*H\\_V3 | AI Strategy Engine*

欢迎使用 H AI Agent

*架构特性：*
- MCP 协议标准化接口
- 多模型热切换（Grok/DeepSeek/OpenAI）
- 插拔式模块设计

输入 /help 查看完整命令列表
或直接 @我 发送消息与 AI 对话"""
        self.tg.send_message(msg, chat_id, reply_to_message_id=message_id)

    def cmd_help(self, chat_id: int, message_id: int = None):
        msg = """*命令列表*

*信号类：*
/scan - 全币种扫描
/signal - 最佳交易信号
/btc /eth /sol /doge /okb - 单币种分析

*分析类：*
/sentiment - 市场情绪分析

*系统类：*
/status - 系统状态
/version - 版本信息
/providers - AI 模型列表

*AI 对话：*
直接 @我 发送任何消息即可与 AI 交流
支持自动识别币种并注入引擎数据"""
        self.tg.send_message(msg, chat_id, reply_to_message_id=message_id)

    def cmd_scan(self, chat_id: int, message_id: int = None):
        """全币种扫描"""
        self.tg.send_message("H AI Agent 正在扫描...", chat_id, reply_to_message_id=message_id)

        results = []
        lines = ["*H\\_V3 全币种扫描*\n"]

        for sym in SYMBOLS:
            try:
                result = scan_symbol(sym)
                results.append(result)

                if result.get("error"):
                    lines.append(f"x {sym}: {result.get('message', '未知错误')}")
                    continue

                dir_map = {"long": "做多", "short": "做空", "neutral": "观望"}
                direction = dir_map.get(result["direction"], "未知")

                lines.append(
                    f"*{sym}* | {direction} | "
                    f"评分:{result['score']} | "
                    f"H:{result['hurst']:.3f} | "
                    f"RSI:{result['rsi']:.0f}"
                )

                if result["direction"] != "neutral":
                    lines.append(
                        f"  入场:{result['entry_price']:,.2f} "
                        f"止盈:{result['tp_price']:,.2f} "
                        f"止损:{result['sl_price']:,.2f}"
                    )
            except Exception as e:
                lines.append(f"x {sym}: 扫描异常 {e}")
                print(f"[扫描异常] {sym}: {e}")

        lines.append(f"\n{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        lines.append("H\\_V3 Engine | MCP Protocol")
        self.tg.send_message("\n".join(lines), chat_id)

    def cmd_signal(self, chat_id: int, message_id: int = None):
        """推送最佳信号"""
        self.tg.send_message("H AI Agent 正在寻找最佳交易机会...", chat_id, reply_to_message_id=message_id)

        best = None
        best_score = 0

        for sym in SYMBOLS:
            try:
                result = scan_symbol(sym)
                if result.get("error"):
                    continue
                score = abs(result.get("score", 0))
                if score > best_score and result["direction"] != "neutral":
                    best_score = score
                    best = result
            except Exception as e:
                print(f"[信号异常] {sym}: {e}")

        if not best:
            self.tg.send_message("当前无明确交易信号，建议观望。", chat_id)
            return

        dir_cn = "做多" if best["direction"] == "long" else "做空"
        msg = f"""*最佳信号: {best['symbol']}*

方向: {dir_cn}
评分: {best['score']}/5
入场: {best['entry_price']:,.2f} USDT
止盈: {best['tp_price']:,.2f} USDT
止损: {best['sl_price']:,.2f} USDT

*指标数据:*
赫斯特: {best['hurst']:.4f} ({best['market_state']})
RSI: {best['rsi']:.1f}
ATR: {best['atr']:.2f}
风险: {best['risk_level']}

理由: {best['reason']}

{datetime.now().strftime('%Y-%m-%d %H:%M')}
H\\_V3 Engine"""
        self.tg.send_message(msg, chat_id)

    def cmd_symbol(self, symbol: str, chat_id: int, message_id: int = None):
        """单币种分析"""
        self.tg.send_message(f"H AI Agent 正在分析 {symbol}...", chat_id, reply_to_message_id=message_id)

        try:
            result = scan_symbol(symbol)
        except Exception as e:
            self.tg.send_message(f"{symbol} 分析失败: {e}", chat_id)
            print(f"[分析异常] {symbol}: {e}")
            traceback.print_exc()
            return

        if result.get("error"):
            self.tg.send_message(f"{symbol} 分析失败: {result.get('message')}", chat_id)
            return

        dir_map = {"long": "做多", "short": "做空", "neutral": "观望"}
        direction = dir_map.get(result["direction"], "未知")

        msg = f"""*{symbol} 分析*

方向: {direction}
评分: {result['score']}/5
当前价: {result['entry_price']:,.2f} USDT"""

        if result["direction"] != "neutral":
            msg += f"""
止盈: {result['tp_price']:,.2f} USDT
止损: {result['sl_price']:,.2f} USDT"""

        msg += f"""

*技术指标:*
赫斯特: {result['hurst']:.4f} ({result['market_state']})
RSI: {result['rsi']:.1f}
EMA: fast={result['ema_fast']:,.2f} / slow={result['ema_slow']:,.2f}
MACD柱: {result['macd_histogram']:.4f}
ATR: {result['atr']:.2f}
风险: {result['risk_level']}

理由: {result['reason']}

{datetime.now().strftime('%Y-%m-%d %H:%M')}
H\\_V3 Engine | MCP Protocol"""
        self.tg.send_message(msg, chat_id)

    def cmd_sentiment(self, chat_id: int, message_id: int = None):
        """市场情绪分析"""
        self.tg.send_message("H AI Agent 正在分析市场情绪...", chat_id, reply_to_message_id=message_id)

        try:
            tickers = get_tickers(["BTC", "ETH", "SOL"])
            context_parts = []
            for sym, data in tickers.items():
                if not data.get("error"):
                    context_parts.append(f"{sym}: ${data['last_price']:,.0f} ({data['change_24h']:+.1f}%)")

            context = ", ".join(context_parts)
            result = analyze_sentiment("BTC", market_context=context)

            if result.get("error"):
                self.tg.send_message("情绪分析失败，请稍后重试", chat_id)
                return

            score = result.get("score", 0)
            if score >= 0.5:
                sentiment_label = "极度贪婪"
            elif score >= 0.2:
                sentiment_label = "偏多"
            elif score >= -0.2:
                sentiment_label = "中性"
            elif score >= -0.5:
                sentiment_label = "偏空"
            else:
                sentiment_label = "极度恐惧"

            msg = f"""*市场情绪分析*

情绪: {sentiment_label} ({score:+.2f})
置信度: {result.get('confidence', 0):.0%}

{result.get('summary', '')}

*关键因素:*
"""
            for factor in result.get("key_factors", [])[:5]:
                msg += f"- {factor}\n"

            msg += f"\n行情: {context}"
            msg += f"\n\nH\\_V3 AI | {result.get('provider', 'Grok')}"
            self.tg.send_message(msg, chat_id)

        except Exception as e:
            self.tg.send_message(f"情绪分析出错: {e}", chat_id)
            print(f"[情绪分析异常] {e}")
            traceback.print_exc()

    def cmd_status(self, chat_id: int, message_id: int = None):
        """系统状态"""
        uptime = time.time() - START_TIME
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)

        msg = f"""*H\\_V3 系统状态*

运行时间: {hours}h {minutes}m
进程 PID: {os.getpid()}
架构: MCP Protocol
传输: stdio (同进程)

*MCP Servers:*
- OKX Market: 在线
- Engine: 在线
- AI (Grok): 在线
- Backtest (AI回测验证): 在线

*配置:*
扫描周期: {str(SCAN_HOURS)}h
监控币种: {', '.join(SYMBOLS)}
推送群: {TELEGRAM_CHAT_ID}"""
        self.tg.send_message(msg, chat_id, reply_to_message_id=message_id)

    def cmd_version(self, chat_id: int, message_id: int = None):
        msg = """*H\\_V3 | H AI Agent*

版本: 3.0.0
架构: MCP Protocol (Model Context Protocol)
引擎: 多因子评分 + 赫斯特指数
AI: Grok (可热切换 DeepSeek/OpenAI)
数据: OKX V5 API

*MCP Servers:*
- h\\_v3\\_mcp\\_okx\\_market (行情)
- h\\_v3\\_mcp\\_engine (技术面)
- h\\_v3\\_mcp\\_backtest (AI回测验证)
- h\\_v3\\_mcp\\_ai (AI对话)

*设计理念:*
插拔式架构，任何模块可秒级替换"""
        self.tg.send_message(msg, chat_id, reply_to_message_id=message_id)

    def cmd_providers(self, chat_id: int, message_id: int = None):
        """列出 AI 模型提供商"""
        try:
            providers = list_providers()
            lines = ["*AI 模型提供商*\n"]
            for key, info in providers.items():
                if info["is_active"]:
                    status = "当前使用"
                elif info["available"]:
                    status = "可用"
                else:
                    status = "未配置"
                lines.append(f"*{info['name']}*: {status}")
                lines.append(f"  模型: `{info['models']['default']}`")
            self.tg.send_message("\n".join(lines), chat_id, reply_to_message_id=message_id)
        except Exception as e:
            self.tg.send_message(f"获取模型列表失败: {e}", chat_id)

    # ============================================================
    # AI 对话处理
    # ============================================================

    def handle_ai_chat(self, text: str, chat_id: int, message_id: int = None):
        """处理非命令消息：AI 对话（自动注入引擎数据）"""
        # 先发送即时反馈，引用提问者的消息
        self.tg.send_message("H AI Agent 正在分析...", chat_id, reply_to_message_id=message_id)

        try:
            # 去掉 @bot 前缀，提取纯文本
            clean_text = text.replace(f"@{BOT_USERNAME}", "").strip()
            if not clean_text:
                # 只 @ 不说话，直接回复欢迎语
                welcome = """我是 H AI Agent，专注合约策略分析与策略回测。

你可以问我：
- 任何币种的行情和趋势判断
- 具体的开仓建议和止盈止损
- 策略回测验证
- 市场情绪分析

试试发：@H\_NO\_1\_bot BTC能做多吗？

H AI Agent | MCP Protocol"""
                self.tg.send_message(welcome, chat_id)
                return

            # 检测用户提到的币种
            detected_symbol = self._detect_symbol(clean_text)

            # 如果检测到币种，先获取引擎数据
            engine_data = None
            if detected_symbol:
                try:
                    engine_data = scan_symbol(detected_symbol)
                    if engine_data.get("error"):
                        engine_data = None
                except Exception as e:
                    print(f"[引擎异常] {detected_symbol}: {e}")
                    engine_data = None

            # 调用 AI 对话
            result = chat(clean_text, engine_data=engine_data)
            response = result.get("response", "抱歉，AI 暂时无法回答。")

            # 构建回复（加水印）
            watermark = self._build_watermark(engine_data, result)
            full_response = f"{response}\n\n{watermark}"

            self.tg.send_message(full_response, chat_id)

        except Exception as e:
            error_msg = f"AI 分析出错，请稍后重试\n错误: {str(e)[:100]}"
            self.tg.send_message(error_msg, chat_id)
            print(f"[AI对话异常] {e}")
            traceback.print_exc()

    def _detect_symbol(self, text: str) -> str:
        """从用户消息中检测币种"""
        text_lower = text.lower()
        for symbol, aliases in SYMBOL_ALIASES.items():
            for alias in aliases:
                if alias in text_lower:
                    return symbol
        return ""

    def _build_watermark(self, engine_data: dict, ai_result: dict) -> str:
        """构建信号水印 - 简洁版"""
        if not engine_data or engine_data.get("error"):
            return "\n———————————————\n_H AI Agent_"
        symbol = engine_data.get("symbol", "")
        score = engine_data.get("score", 0)
        direction = engine_data.get("direction", "neutral")
        entry = engine_data.get("entry_price", 0)
        tp = engine_data.get("tp_price", 0)
        sl = engine_data.get("sl_price", 0)
        atr = engine_data.get("atr", 0)
        hurst = engine_data.get("hurst", 0)
        rsi = engine_data.get("rsi", 0)
        risk = engine_data.get("risk_level", "中")
        market = engine_data.get("market_state", "")
        # 方向
        dir_map = {"long": "做多 ▲", "short": "做空 ▼", "neutral": "观望 ◆"}
        dir_cn = dir_map.get(direction, "观望 ◆")
        # 星级
        if direction != "neutral":
            stars = min(5, max(1, int(abs(score) + 0.5)))
            star_str = "⭐️" * stars
        else:
            star_str = ""
        # 支撑位
        if direction == "long" and atr > 0:
            support = entry - atr * 2.0
        elif direction == "short" and atr > 0:
            support = entry + atr * 2.0
        else:
            support = 0
        # 格式化价格
        if entry >= 1000:
            fmt = ",.0f"
        elif entry >= 1:
            fmt = ",.2f"
        else:
            fmt = ",.4f"
        lines = []
        lines.append("")
        lines.append("———————————————")
        # 大标题
        if star_str:
            lines.append(f"*{symbol} | {dir_cn}*  {star_str}")
        else:
            lines.append(f"*{symbol} | {dir_cn}*")
        lines.append("")
        # 价格区
        lines.append(f"当前价格  `{entry:{fmt}}`")
        if direction != "neutral" and tp > 0:
            lines.append(f"目标位    `{tp:{fmt}}`")
            lines.append(f"止损位    `{sl:{fmt}}`")
            lines.append(f"支撑位    `{support:{fmt}}`")
        else:
            lines.append("_信号强度不足，建议观望_")
        lines.append("")
        lines.append("———————————————")
        # 指标区
        lines.append(f"RSI `{rsi:.0f}` | H指数 `{hurst:.2f}` | {market}")
        lines.append(f"风险等级: *{risk}*")
        # 聪明钱
        sm = engine_data.get("smart_money", {})
        if sm and sm.get("available"):
            sm_dir = {"long": "做多", "short": "做空", "neutral": "观望"}.get(sm.get("direction", ""), "观望")
            sm_conf = {"high": "强", "medium": "中", "low": "弱"}.get(sm.get("confidence", ""), "")
            sm_long = sm.get("long_pct", 0)
            lines.append(f"聪明钱: {sm_conf}共识{sm_dir} (多{sm_long:.0f}%)")
        lines.append("")
        lines.append("———————————————")
        lines.append("_H AI Agent | 三维度引擎 | 4H_")
        return "\n".join(lines)
# ============================================================
# 定时扫描线程
# ============================================================

class SchedulerThread(threading.Thread):
    """定时扫描并推送信号"""

    def __init__(self, telegram: TelegramClient):
        super().__init__(daemon=True)
        self.tg = telegram
        self.running = True

    def run(self):
        print("[调度] 定时扫描线程已启动")
        print(f"[调度] 推送时间(北京): {SCAN_HOURS}")
        while self.running:
            # 计算距离下一个推送时间点的秒数
            now = datetime.now()  # VPS 已设为 CST (UTC+8)
            current_hour = now.hour
            current_min = now.minute
            # 找下一个推送时间
            next_hour = None
            for h in SCAN_HOURS:
                if h > current_hour or (h == current_hour and current_min < 1):
                    next_hour = h
                    break
            if next_hour is None:
                # 今天的都过了，等明天第一个
                next_hour = SCAN_HOURS[0]
                wait_hours = (24 - current_hour) + next_hour
            else:
                wait_hours = next_hour - current_hour
            wait_seconds = wait_hours * 3600 - now.minute * 60 - now.second
            if wait_seconds <= 0:
                wait_seconds = 60  # 防止负数
            print(f"[调度] 下次推送: {next_hour:02d}:00 (等待{wait_seconds//60}分钟)")
            # 分段 sleep，每 60 秒检查一次是否需要退出
            for _ in range(wait_seconds // 60 + 1):
                if not self.running:
                    return
                time.sleep(min(60, wait_seconds))
                wait_seconds -= 60
                if wait_seconds <= 0:
                    break
            if not self.running:
                break
            try:
                self._scheduled_scan()
            except Exception as e:
                print(f"[调度错误] {e}")
                traceback.print_exc()

    def _scheduled_scan(self):
        """执行定时扫描"""
        print(f"[调度] 开始定时扫描 {datetime.now().strftime('%H:%M')}")

        results = []
        for sym in SYMBOLS:
            try:
                result = scan_symbol(sym)
                results.append(result)
            except Exception as e:
                print(f"[调度扫描异常] {sym}: {e}")

        # 只推送有明确信号的
        signals = [r for r in results if not r.get("error") and r.get("direction") != "neutral"]
        if signals:
            lines = []
            lines.append("*H AI Agent | 策略信号*")
            lines.append(f"_{datetime.now().strftime('%Y-%m-%d %H:%M')} | 4H周期_")
            lines.append("")
            lines.append("———————————————")
            for s in signals:
                dir_cn = "做多 ▲" if s["direction"] == "long" else "做空 ▼"
                score = abs(s.get("score", 0))
                stars = min(5, max(1, int(score + 0.5)))
                star_str = "⭐️" * stars
                entry = s["entry_price"]
                tp = s["tp_price"]
                sl = s["sl_price"]
                atr = s.get("atr", 0)
                risk = s.get("risk_level", "中")
                # 支撑位
                if s["direction"] == "long" and atr > 0:
                    support = entry - atr * 2.0
                else:
                    support = sl
                # 格式化
                if entry >= 1000:
                    fmt = ",.0f"
                elif entry >= 1:
                    fmt = ",.2f"
                else:
                    fmt = ",.4f"
                # 聪明钱
                sm = s.get("smart_money", {})
                sm_text = ""
                if sm and sm.get("available") and sm.get("direction") != "neutral":
                    sm_dir_cn = {"long": "多", "short": "空"}.get(sm.get("direction", ""), "")
                    sm_pct = sm.get("long_pct", 0) if sm.get("direction") == "long" else sm.get("short_pct", 0)
                    sm_text = f"\n聪明钱: {sm_dir_cn}({sm_pct:.0f}%)"
                lines.append("")
                lines.append(f"*{s['symbol']}* | {dir_cn}  {star_str}")
                lines.append(f"当前 `{entry:{fmt}}`")
                lines.append(f"目标 `{tp:{fmt}}` | 止损 `{sl:{fmt}}`")
                lines.append(f"支撑 `{support:{fmt}}` | 风险 *{risk}*{sm_text}")
            lines.append("")
            lines.append("———————————————")
            lines.append("_H AI Agent | 三维度引擎_")
            self.tg.send_message("\n".join(lines))
    def stop(self):
        self.running = False


# ============================================================
# 主程序
# ============================================================

START_TIME = time.time()


def main():
    # 1. 获取进程锁
    lock_fp = acquire_lock()

    # 2. 初始化
    print("=" * 60)
    print("  H_V3 | H AI Agent")
    print("  版本: 3.0.0")
    print("  架构: MCP Protocol")
    print(f"  AI: Grok")
    print(f"  币种: {', '.join(SYMBOLS)}")
    print("=" * 60)

    telegram = TelegramClient(TELEGRAM_TOKEN)
    router = CommandRouter(telegram)
    scheduler = SchedulerThread(telegram)

    # 3. 优雅退出
    def shutdown(signum, frame):
        print("\n[退出] 收到终止信号，正在清理...")
        scheduler.stop()
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # 4. 启动定时扫描
    scheduler.start()
    print("[启动] 定时扫描线程已启动")

    # 5. 开始消息循环
    print("[启动] 开始监听消息...")
    while True:
        try:
            updates = telegram.get_updates()
            for update in updates:
                telegram.offset = update["update_id"] + 1

                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = msg.get("chat", {}).get("id")
                message_id = msg.get("message_id")  # 用于引用回复

                if not text or not chat_id:
                    continue

                # 判断是否需要响应
                chat_type = msg.get("chat", {}).get("type", "private")
                is_private = chat_type == "private"
                is_mentioned = f"@{BOT_USERNAME}" in text
                is_command = text.startswith("/")

                # 群聊中只响应 @ 提及和命令，私聊全部响应
                if not is_private and not is_mentioned and not is_command:
                    continue

                # 打印日志
                user = msg.get("from", {}).get("first_name", "未知")
                print(f"[处理] {user}: {text[:50]}")

                # 路由处理
                if is_command:
                    router.route(text, chat_id, text, message_id)
                else:
                    router.route("", chat_id, text, message_id)

        except Exception as e:
            print(f"[主循环异常] {e}")
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
