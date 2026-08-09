<script setup lang="ts">
// 成交记录表。展示每笔成交的方向/数量/价格/费用/盈亏。

import type { Trade } from '../types'

defineProps<{
  trades: Trade[]
}>()

function fmtDate(s: string): string {
  return s.slice(0, 10)
}
function fmtNum(v: number, digits = 2): string {
  return Number.isFinite(v) ? v.toFixed(digits) : '-'
}
function directionLabel(direction: Trade['direction']): string {
  return direction === 'BUY' ? '买入' : '卖出'
}
function sourceLabel(trade: Trade): string {
  if (trade.source === 'stop') return '风控触发'
  if (trade.stop_loss != null || trade.take_profit != null) {
    return trade.direction === 'BUY' ? '建仓+风控' : '移动止损'
  }
  return '策略信号'
}
</script>

<template>
  <div class="trade-table-wrap">
    <p v-if="trades.length === 0" class="empty">无成交记录</p>
    <table v-else class="trade-table">
      <thead>
        <tr>
          <th>日期</th>
          <th>方向</th>
          <th>信号</th>
          <th class="num">数量</th>
          <th class="num">价格</th>
          <th class="num">止损参考</th>
          <th class="num">止盈参考</th>
          <th class="num">手续费</th>
          <th class="num">盈亏</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(t, i) in trades" :key="i" :class="{ rejected: t.rejected }">
          <td>{{ fmtDate(t.datetime) }}</td>
          <td :class="t.direction">{{ directionLabel(t.direction) }}</td>
          <td>
            <span class="source" :class="{ risk: t.source === 'stop' || t.stop_loss != null }">
              {{ sourceLabel(t) }}
            </span>
          </td>
          <td class="num">{{ fmtNum(t.size, 0) }}</td>
          <td class="num">{{ fmtNum(t.price, 3) }}</td>
          <td class="num risk-price">{{ t.stop_loss == null ? '-' : fmtNum(t.stop_loss, 3) }}</td>
          <td class="num risk-price take-profit">
            {{ t.take_profit == null ? '-' : fmtNum(t.take_profit, 3) }}
          </td>
          <td class="num muted">{{ fmtNum(t.commission, 2) }}</td>
          <td class="num" :class="{ pos: t.pnl > 0, neg: t.pnl < 0 }">
            {{ t.pnl === 0 ? '-' : fmtNum(t.pnl, 2) }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.trade-table-wrap {
  overflow-x: auto;
}
.empty {
  color: var(--text-dim);
  font-size: 13px;
  padding: 16px 0;
}
.trade-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.trade-table th,
.trade-table td {
  padding: 7px 10px;
  text-align: left;
  border-bottom: 1px solid var(--border);
}
.trade-table th {
  color: var(--text-dim);
  font-weight: 600;
  font-size: 12px;
  position: sticky;
  top: 0;
  background: var(--bg-panel);
}
.num {
  text-align: right;
  font-family: var(--font-mono);
}
.muted {
  color: var(--text-dim);
}
.BUY {
  color: var(--up);
  font-weight: 600;
}
.SELL {
  color: var(--down);
  font-weight: 600;
}
.source {
  color: var(--text-dim);
  white-space: nowrap;
}
.source.risk {
  color: #f0b35a;
  font-weight: 600;
}
.risk-price {
  color: #f0b35a;
}
.take-profit {
  color: var(--up);
}
.pos {
  color: var(--up);
}
.neg {
  color: var(--down);
}
.rejected td {
  opacity: 0.4;
  text-decoration: line-through;
}
</style>
