#!/usr/bin/env python3
"""回测测试脚本"""
import sys
sys.path.insert(0, '/root/h_v3')

import h_v3_backtest as bt

# 直接用run_quick接口
print('=== BTC 快速回测 ===')
result_btc = bt.run_quick('BTC')
if result_btc:
    print(f'周期: {result_btc.period_days}天')
    print(f'总交易: {result_btc.total_trades}笔')
    print(f'胜率: {result_btc.win_rate:.1%}')
    print(f'盈亏比: {result_btc.profit_factor:.2f}')
    print(f'总收益: {result_btc.total_return_pct:.2%}')
    print(f'最大回撤: {result_btc.max_drawdown_pct:.2%}')
    print(f'摘要: {result_btc.summary_text}')
else:
    print('回测失败')

print()
print('=== ETH 快速回测 ===')
result_eth = bt.run_quick('ETH')
if result_eth:
    print(f'周期: {result_eth.period_days}天')
    print(f'总交易: {result_eth.total_trades}笔')
    print(f'胜率: {result_eth.win_rate:.1%}')
    print(f'盈亏比: {result_eth.profit_factor:.2f}')
    print(f'总收益: {result_eth.total_return_pct:.2%}')
    print(f'最大回撤: {result_eth.max_drawdown_pct:.2%}')
    print(f'摘要: {result_eth.summary_text}')
else:
    print('回测失败')

print()
print('=== SOL 快速回测 ===')
result_sol = bt.run_quick('SOL')
if result_sol:
    print(f'周期: {result_sol.period_days}天')
    print(f'总交易: {result_sol.total_trades}笔')
    print(f'胜率: {result_sol.win_rate:.1%}')
    print(f'盈亏比: {result_sol.profit_factor:.2f}')
    print(f'总收益: {result_sol.total_return_pct:.2%}')
    print(f'最大回撤: {result_sol.max_drawdown_pct:.2%}')
    print(f'摘要: {result_sol.summary_text}')
else:
    print('回测失败')

# 查看缓存的绩效数据
print()
print('=== 缓存绩效 ===')
perf = bt.get_performance_data()
if perf:
    for sym, data in perf.items():
        print(f'{sym}: {data}')
else:
    print('无缓存绩效数据')
