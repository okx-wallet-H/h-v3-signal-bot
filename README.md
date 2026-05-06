# H V3 - AI 合约策略信号系统

四层架构的加密货币合约交易信号系统，集成 OKX Agent Trade Kit + Grok AI。

## 架构

```
L1 数据接口层 (h_v3_data_api.py)
    ↓
L2 策略引擎 (h_v3_strategy.py) - 8因子加权评分
    ↓
L3 回测验证 (h_v3_backtest.py) - 100天历史回测
    ↓
L4 Bot推送 (h_v3_bot_v2.py) - Telegram Bot + Grok AI
```

## 核心模块

| 文件 | 功能 |
|------|------|
| `h_v3_data_api.py` | 数据接口层：OKX CLI + V5 REST API，聪明钱三层信号 |
| `h_v3_strategy.py` | 策略引擎：8因子（趋势/动量/MACD/布林/资金流/市场结构/聪明钱/多TF） |
| `h_v3_backtest.py` | 回测引擎：100天4H K线，参数优化后胜率64%+ |
| `h_v3_bot_v2.py` | Telegram Bot：信号推送 + Grok AI 自然语言分析 |
| `h_v3_bot.py` | Bot旧版（备份） |
| `h_v3_config.py` | 配置文件 |

## MCP 模块（早期版本）

| 文件 | 功能 |
|------|------|
| `h_v3_mcp_engine.py` | MCP策略引擎 |
| `h_v3_mcp_okx_market.py` | MCP行情数据 |
| `h_v3_mcp_smartmoney.py` | MCP聪明钱 |
| `h_v3_mcp_backtest.py` | MCP回测 |
| `h_v3_mcp_ai.py` | MCP AI分析 |

## 测试 & 工具

| 文件 | 功能 |
|------|------|
| `bt_optimize.py` | 参数网格搜索优化 |
| `test_*.py` | 各模块测试脚本 |
| `optimize_results.json` | 优化结果数据 |

## 部署

```bash
# systemd service
cp h_v3.service /etc/systemd/system/
systemctl daemon-reload
systemctl start h_v3
```

## 回测结果（优化后）

| 币种 | 交易数 | 胜率 | 盈亏比 | 收益 | Sharpe |
|------|--------|------|--------|------|--------|
| BTC | 11 | 64% | 1.51 | +3.2% | 2.71 |
| ETH | 9 | 67% | 1.94 | +5.9% | 4.14 |
| SOL | 9 | 67% | 2.07 | +7.5% | 4.15 |

## 状态

⚠️ 开发中，信号系统正在调优，暂未上线推送。
