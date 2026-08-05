<script setup lang="ts">
// 策略库页面：列出用户保存的策略，支持「载入」（回填到对应回测页）+「删除」，
// 以及「组合回测」——勾选多个策略，各拿 1/N 资金、各跑原标的，看综合表现。
// 数据来自后端 SQLite（GET /api/v1/strategies）。空态提示去回测页保存。

import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import EquityChart from '../components/EquityChart.vue'
import GradeDetails from '../components/GradeDetails.vue'
import KlineChart from '../components/KlineChart.vue'
import MetricTable from '../components/MetricTable.vue'
import PortfolioCompareChart from '../components/PortfolioCompareChart.vue'
import PortfolioSummaryTable from '../components/PortfolioSummaryTable.vue'
import TradeTable from '../components/TradeTable.vue'
import {
  deleteSavedStrategy,
  fetchSavedStrategies,
  formatError,
  renameSavedStrategy,
  saveStrategy,
} from '../api'
import { gradePortfolio } from '../grading'
import { detectExMarket, detectMarket, isExMarketCode } from '../market'
import type { MultiStrategyItem, Performance, SavedStrategy, Trade } from '../types'
import { useBacktestStore } from '../stores/backtest'

const router = useRouter()
const store = useBacktestStore()

const strategies = ref<SavedStrategy[]>([])
const loading = ref(false)
const error = ref('')
const deletingId = ref<string | null>(null)

// ── 组合改名弹窗 ────────────────────────────────────────────────────────────
const renameOpen = ref(false)
const renameTarget = ref<SavedStrategy | null>(null)
const renameName = ref('')
const renameLoading = ref(false)
const renameNameRef = ref<HTMLInputElement | null>(null)

// ── Tab 分类：单标的 / 组合 ────────────────────────────────────────────────────
// single tab = kind='single'；combo tab = kind='portfolio' | 'multi'
// 默认进单标的；组合回测按钮仅在 single tab 显示（组合策略无法再被组合）
type TabKey = 'single' | 'combo'
const activeTab = ref<TabKey>('single')

const singleStrategies = computed(() => strategies.value.filter((s) => s.kind === 'single'))
const comboStrategies = computed(() => strategies.value.filter((s) => s.kind !== 'single'))

// 切 tab 时清空勾选（避免跨 tab 看不到的勾选残留）
function switchTab(tab: TabKey) {
  if (activeTab.value === tab) return
  activeTab.value = tab
  selectedIds.value = new Set()
}

const visibleStrategies = computed(() =>
  activeTab.value === 'single' ? singleStrategies.value : comboStrategies.value,
)

// ── 保存组合弹窗 ─────────────────────────────────────────────────────────────
const saveComboOpen = ref(false)
const saveComboName = ref('')
const saveComboNotes = ref('')
const saveComboLoading = ref(false)
// 最近一次组合回测使用的 items（保存时复用），由 onComboBacktest 写入
const lastComboItems = ref<MultiStrategyItem[]>([])
const lastComboCash = ref<number>(1_000_000)
// 结果区引用：跑完后滚动定位
const comboResultRef = ref<HTMLElement | null>(null)
// 保存弹窗里名称输入框：打开时自动聚焦
const saveComboNameRef = ref<HTMLInputElement | null>(null)

// 组合结果由 Pinia 保留，但当前页面组件可能被重新创建；优先使用本页刚跑的
// 明细，页面重建时回退到 store 中随回测请求保存的明细。
const comboItemsForSave = computed(() =>
  lastComboItems.value.length > 0 ? lastComboItems.value : store.multiStrategyItems,
)
const comboCashForSave = computed(() =>
  lastComboItems.value.length > 0 ? lastComboCash.value : store.multiStrategyCash,
)

// ── 多策略组合回测：勾选 ─────────────────────────────────────────────────────
const selectedIds = ref<Set<string>>(new Set())

function toggleSelect(id: string) {
  const next = new Set(selectedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedIds.value = next
}

const selectedStrategies = computed(() =>
  strategies.value.filter((s) => selectedIds.value.has(s.id)),
)

function clearSelection() {
  selectedIds.value = new Set()
}

/** 纠正历史保存策略的市场前缀。
 *  早期保存逻辑只按 A 股判断，导致 ETF 或美股 symbol 被错标，
 *  后端按错配市场取到 0 根 K 线被静默跳过。
 *  这里在发请求前统一规范 A 股/美股/港股前缀，纠正历史数据 + 兜底未来。 */
function normalizeSymbol(raw: string): string {
  const value = raw.trim()
  if (!value) return value

  // 已带市场前缀的历史记录必须原样保留扩展市场；否则 US_STOCK:AAPL
  // 会被误改成 SZ:AAPL，直接触发后端 422。
  const colon = value.indexOf(':')
  if (colon > 0) {
    const market = value.slice(0, colon).toUpperCase()
    const code = value.slice(colon + 1).trim()
    if (market === 'US_STOCK') return `US_STOCK:${code.toUpperCase()}`
    if (market === 'HK_MAIN_BOARD') return `HK_MAIN_BOARD:${code}`
    if (market === 'SH' || market === 'SZ' || market === 'BJ') {
      // 早期版本曾把扩展市场代码错误保存成 SZ:QQQ / SZ:00700。
      // 根据代码形态纠正这类历史记录，避免组合请求被 422 拒绝。
      if (/^[A-Za-z]{1,5}$/.test(code)) return `US_STOCK:${code.toUpperCase()}`
      if (/^\d{5}$/.test(code)) return `HK_MAIN_BOARD:${code}`
      return `${market}:${code}`
    }
  }

  // 没有前缀时兼容扩展市场代码（如 AAPL / 00700）和 A 股 6 位代码。
  if (isExMarketCode(value)) {
    const market = detectExMarket(value)
    return `${market}:${market === 'US_STOCK' ? value.toUpperCase() : value}`
  }
  return `${detectMarket(value)}:${value}`
}

/** 组合回测：把勾选的策略组装成 MultiStrategyItem[]，各跑原标的，资金均分。 */
async function onComboBacktest() {
  if (selectedStrategies.value.length === 0) return
  store.error = ''
  // 只取有单标的上下文（symbol）的策略；组合类策略没有单一 symbol，跳过并提示。
  const usable = selectedStrategies.value.filter((s) => s.context?.symbol)
  const skipped = selectedStrategies.value.length - usable.length
  if (usable.length === 0) {
    store.error = '勾选的策略缺少标的上下文（symbol），无法组合回测。请勾选单标的策略。'
    return
  }
  const items: MultiStrategyItem[] = usable.map((s) => ({
    strategy: s.strategy,
    strategy_label: s.strategy_label || s.strategy,
    params: s.params,
    symbol: normalizeSymbol(s.context.symbol as string),
    category: (s.context.category as MultiStrategyItem['category']) || 'DAY',
    start_date: (s.context.start_date as string) || undefined,
    end_date: (s.context.end_date as string) || undefined,
  }))
  lastComboItems.value = items
  lastComboCash.value = 1_000_000
  await store.runMultiStrategy({ items, cash: 1_000_000 })
  if (skipped > 0) {
    store.error = `已跳过 ${skipped} 个缺少单一标的的策略（组合策略无 symbol）。`
  }
}

// ── 保存组合（kind: 'multi'）─────────────────────────────────────────────────

/** 打开保存组合弹窗：预填名称 + 自动聚焦输入框。 */
function openSaveCombo() {
  if (!store.multiStrategyResult) return
  const itemCount = comboItemsForSave.value.length
  if (itemCount === 0) {
    error.value = '无法保存：组合策略明细已丢失，请重新勾选策略并运行组合回测。'
    return
  }
  saveComboName.value = `组合·${itemCount}策略·${new Date().toISOString().slice(0, 10)}`
  saveComboNotes.value = ''
  saveComboOpen.value = true
  // 等弹窗渲染完再聚焦
  nextTick(() => saveComboNameRef.value?.focus())
}

/** ESC 关闭弹窗（绑定在弹窗根元素 @keydown.esc） */
function closeSaveCombo() {
  if (!saveComboLoading.value) saveComboOpen.value = false
}

/** 提交保存组合：把 items + cash 存进 context，组合级绩效存 snapshot。 */
async function submitSaveCombo() {
  if (!store.multiStrategyResult) return
  if (comboItemsForSave.value.length === 0) {
    error.value = '无法保存：组合策略明细已丢失，请重新勾选策略并运行组合回测。'
    saveComboOpen.value = false
    return
  }
  if (!saveComboName.value.trim()) {
    error.value = '请填写组合名称'
    return
  }
  saveComboLoading.value = true
  error.value = ''
  try {
    const tp = store.multiStrategyResult.total_performance
    const created = await saveStrategy({
      name: saveComboName.value.trim(),
      kind: 'multi',
      strategy: 'multi',
      strategy_label: `${comboItemsForSave.value.length} 策略组合`,
      context: {
        items: comboItemsForSave.value,
        cash: comboCashForSave.value,
      },
      trade_config: { cash: comboCashForSave.value },
      snapshot: {
        total_return: tp.total_return,
        annual_return: tp.annual_return,
        total_stocks: tp.total_stocks,
        total_cash: tp.total_cash,
      },
      tags: ['组合'],
      notes: saveComboNotes.value.trim(),
    })
    strategies.value = [created, ...strategies.value]
    saveComboOpen.value = false
  } catch (e) {
    error.value = formatError(e)
  } finally {
    saveComboLoading.value = false
  }
}

// ── 载入组合（kind: 'multi'）→ 自动重跑到今天 ────────────────────────────────

function isoToday(): string {
  return new Date().toISOString().slice(0, 10)
}

/** 载入组合：把保存的 items 的 end_date 全部覆盖为今天，自动触发组合回测。
 *  这样跑出来的"当前持仓"= 截至今天的策略信号（哪些该买/该卖）。 */
async function onLoadMulti(s: SavedStrategy) {
  const ctx = s.context || {}
  const rawItems = Array.isArray(ctx.items) ? (ctx.items as MultiStrategyItem[]) : []
  if (rawItems.length === 0) {
    error.value = '该组合没有保存策略明细（items），可能数据损坏。'
    return
  }
  // 运行时校验：每条至少要有 strategy + symbol，否则带病跑到后端才报错
  const valid = rawItems.every(
    (it) => it && typeof it.strategy === 'string' && typeof it.symbol === 'string',
  )
  if (!valid) {
    error.value = '组合数据损坏：部分策略缺少 strategy 或 symbol 字段。'
    return
  }
  if (!confirm(
    `载入「${s.name}」并用今天（${isoToday()}）重跑 ${rawItems.length} 个策略？\n\n` +
    `结果区会显示截至今天的策略信号（"哪些该买/该卖"）。`,
  )) return
  const today = isoToday()
  const items = rawItems.map((it) => ({
    ...it,
    symbol: normalizeSymbol(it.symbol),
    end_date: today,
  }))
  const cash = typeof ctx.cash === 'number' ? ctx.cash : 1_000_000
  lastComboItems.value = items
  lastComboCash.value = cash
  await store.runMultiStrategy({ items, cash })
  // 跑完回到报告顶部，先看当前信号与核心指标。
  await nextTick()
  comboResultRef.value?.scrollIntoView({
    behavior: 'smooth',
    block: 'start',
  })
}

onMounted(load)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const resp = await fetchSavedStrategies()
    strategies.value = resp.strategies
  } catch (e) {
    error.value = formatError(e)
  } finally {
    loading.value = false
  }
}

/** 载入：把保存的策略 + 标的上下文塞进 URL query，跳转对应回测页（页面 onMounted 时回填）。 */
function onLoad(s: SavedStrategy) {
  const ctx = s.context
  const params = JSON.stringify(s.params)
  if (s.kind === 'portfolio') {
    router.push({
      path: '/portfolio',
      query: {
        strategy: s.strategy,
        params,
        stocks: Array.isArray(ctx.stocks) ? (ctx.stocks as string[]).join(',') : '',
        startDate: (ctx.start_date as string) || undefined,
        endDate: (ctx.end_date as string) || undefined,
        category: (ctx.category as string) || undefined,
      },
    })
  } else {
    // 保存的 symbol 带"市场:6位代码"前缀（如 SH:601088，便于策略库展示），
    // 但回测页 SymbolPicker 的 code 只接受纯 6 位数字（市场由 detectMarket 自动识别），
    // 故载入时剥掉前缀，只传 6 位代码。
    const rawSymbol = (ctx.symbol as string) || ''
    const codeOnly = rawSymbol.includes(':') ? rawSymbol.split(':').pop()! : rawSymbol
    router.push({
      path: '/',
      query: {
        strategy: s.strategy,
        params,
        symbol: codeOnly || undefined,
        startDate: (ctx.start_date as string) || undefined,
        endDate: (ctx.end_date as string) || undefined,
        category: (ctx.category as string) || undefined,
      },
    })
  }
}

async function onDelete(s: SavedStrategy) {
  if (!confirm(`确定删除「${s.name}」？此操作不可撤销。`)) return
  deletingId.value = s.id
  try {
    await deleteSavedStrategy(s.id)
    strategies.value = strategies.value.filter((x) => x.id !== s.id)
  } catch (e) {
    error.value = formatError(e)
  } finally {
    deletingId.value = null
  }
}

function openRename(s: SavedStrategy) {
  renameTarget.value = s
  renameName.value = s.name
  error.value = ''
  renameOpen.value = true
  nextTick(() => renameNameRef.value?.focus())
}

function closeRename() {
  if (renameLoading.value) return
  renameOpen.value = false
  renameTarget.value = null
  error.value = ''
}

async function submitRename() {
  const target = renameTarget.value
  const name = renameName.value.trim()
  if (!target) return
  if (!name) {
    error.value = '请输入组合名称'
    return
  }
  renameLoading.value = true
  error.value = ''
  try {
    const updated = await renameSavedStrategy(target.id, name)
    strategies.value = strategies.value.map((item) =>
      item.id === updated.id ? updated : item,
    )
    renameOpen.value = false
    renameTarget.value = null
  } catch (e) {
    error.value = formatError(e)
  } finally {
    renameLoading.value = false
  }
}

// ── 展示辅助 ────────────────────────────────────────────────────────────────

function pct(v: unknown): string {
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? `${(n * 100).toFixed(2)}%` : '-'
}
function num(v: unknown, d = 2): string {
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n.toFixed(d) : '-'
}
function ctxLabel(s: SavedStrategy): string {
  const ctx = s.context
  if (s.kind === 'multi') {
    const items = Array.isArray(ctx.items) ? (ctx.items as MultiStrategyItem[]) : []
    return items.length ? `${items.length} 策略 · ${items.map((i) => i.symbol).slice(0, 3).join(' ')}${items.length > 3 ? ' …' : ''}` : '-'
  }
  if (s.kind === 'portfolio') {
    const stocks = Array.isArray(ctx.stocks) ? (ctx.stocks as string[]) : []
    return stocks.length ? `${stocks.length} 只：${stocks.slice(0, 3).join(' ')}${stocks.length > 3 ? ' …' : ''}` : '-'
  }
  return (ctx.symbol as string) || '-'
}
function dateRange(s: SavedStrategy): string {
  const ctx = s.context
  const s0 = (ctx.start_date as string) || ''
  const s1 = (ctx.end_date as string) || ''
  if (!s0 && !s1) return '-'
  return `${s0 || '?'} ~ ${s1 || '?'}`
}
function createdShort(s: SavedStrategy): string {
  // created_at 形如 "2026-07-04T15:30:22Z"，截到分钟
  return (s.created_at || '').replace('T', ' ').replace(/:\d{2}Z?$/, '').slice(0, 16)
}

// ── 组合回测：当前持仓（回测结束时各策略的持仓快照）────────────────────────────
// positions 是每根 K 线一行的快照序列，取最后一行 = 回测结束时的持仓。
// size > 0 表示该策略结束仍持有，size ≈ 0 表示已清仓。

interface Holding {
  key: string // 策略槽位 key，如 "双均线交叉@SH:601088"
  strategyLabel: string
  symbol: string
  size: number // 持仓数量（0 = 已清仓）
  avgPrice: number // 持仓成本
  marketValue: number // 市值
  unrealizedPnl: number // 未实现盈亏（元）
  unrealizedPct: number // 未实现收益率
  holding: boolean // 是否在持仓中
}

const holdings = computed<Holding[]>(() => {
  const res = store.multiStrategyResult
  if (!res) return []
  const out: Holding[] = []
  for (const [key, br] of Object.entries(res.individual_results)) {
    const positions = br.positions as Array<Record<string, unknown>>
    if (!Array.isArray(positions) || positions.length === 0) continue
    const last = positions[positions.length - 1]
    const size = Number(last.size ?? 0)
    const avgPrice = Number(last.avg_price ?? 0)
    const marketValue = Number(last.market_value ?? 0)
    const unrealizedPnl = Number(last.unrealized_pnl ?? 0)
    const [strategyLabel, symbol] = key.split('@')
    out.push({
      key,
      strategyLabel: strategyLabel || key,
      symbol: symbol || '',
      size,
      avgPrice,
      marketValue,
      unrealizedPnl,
      unrealizedPct: avgPrice > 0 ? unrealizedPnl / (avgPrice * Math.abs(size)) : 0,
      holding: size > 0.5, // 容忍浮点误差
    })
  }
  return out
})

const holdingCount = computed(() => holdings.value.filter((h) => h.holding).length)
const waitingCount = computed(() => holdings.value.length - holdingCount.value)
const profitableHoldingCount = computed(
  () => holdings.value.filter((h) => h.holding && h.unrealizedPnl >= 0).length,
)

const resultStartDate = computed(() => {
  const equity = store.multiStrategyResult?.combined_equity || []
  return equity.length > 0 ? equity[0].datetime.slice(0, 10) : '-'
})

const resultEndDate = computed(() => {
  const equity = store.multiStrategyResult?.combined_equity || []
  return equity.length > 0 ? equity[equity.length - 1].datetime.slice(0, 10) : '-'
})

const signalHeadline = computed(() => {
  const total = holdings.value.length
  if (total === 0) return '暂无模型仓位数据'
  if (holdingCount.value === 0) return '当前全部空仓，等待买点'
  if (holdingCount.value === total) return `${total} 个策略均在持仓`
  return `${holdingCount.value} 个策略持仓，${waitingCount.value} 个等待买点`
})

const signalDescription = computed(() => {
  if (holdings.value.length === 0) return '请检查本次回测是否返回持仓快照'
  if (holdingCount.value === 0) return '目前没有策略要求持有标的，不代表未来不会出现买点'
  return `持仓中 ${profitableHoldingCount.value} 个浮盈，${holdingCount.value - profitableHoldingCount.value} 个浮亏`
})

const signalTone = computed(() => {
  if (holdings.value.length === 0 || holdingCount.value === 0) return 'cash'
  return waitingCount.value === 0 ? 'active' : 'mixed'
})

// ── 组合回测：历史成交与分策略 K 线买卖点 ────────────────────────────────────
// 后端在 individual_results[*] 中同时返回 trades 和本次回测使用的 bars。
// 按策略拆开展示，避免同一标的挂多套策略时把信号混在一张图里。
const strategyTradeResults = computed(() => {
  const results = store.multiStrategyResult?.individual_results || {}
  return Object.entries(results).map(([key, result]) => ({
    key,
    trades: Array.isArray(result.trades) ? result.trades : [],
    bars: Array.isArray(result.bars) ? result.bars : [],
  }))
})

const totalTradeCount = computed(() =>
  strategyTradeResults.value.reduce((sum, item) => sum + item.trades.length, 0),
)

interface CombinedTradeRow extends Trade {
  key: string
  strategy: string
  symbol: string
  amount: number
}

const combinedTradeRows = computed<CombinedTradeRow[]>(() => {
  const rows = strategyTradeResults.value.flatMap((item) => {
    const at = item.key.lastIndexOf('@')
    const strategy = at >= 0 ? item.key.slice(0, at) : item.key
    const symbol = at >= 0 ? item.key.slice(at + 1) : ''
    return item.trades.map((trade, index) => ({
      ...trade,
      key: `${item.key}-${trade.datetime}-${index}`,
      strategy,
      symbol,
      amount: trade.size * trade.price,
    }))
  })
  return rows.sort((a, b) => b.datetime.localeCompare(a.datetime))
})

function tradeDirectionLabel(direction: Trade['direction']): string {
  return direction === 'BUY' ? '买入' : '卖出'
}

function tradeDate(datetime: string): string {
  return datetime.slice(0, 10)
}

// 只渲染展开项的 K 线，避免多个长周期策略同时创建大型 ECharts 实例。
const expandedTradeKeys = ref<Set<string>>(new Set())

watch(
  strategyTradeResults,
  (items) => {
    expandedTradeKeys.value = new Set(items.length > 0 ? [items[0].key] : [])
  },
  { immediate: true },
)

function onTradeGroupToggle(event: Event, key: string) {
  const details = event.currentTarget as HTMLDetailsElement
  const next = new Set(expandedTradeKeys.value)
  if (details.open) next.add(key)
  else next.delete(key)
  expandedTradeKeys.value = next
}

// 持仓三态视图：把 statusClass / label / rowClass 一次性算好，模板只读不调函数。
// 否则每行 ×3 次函数调用 + holdingRowClass 返回新对象会触发 Vue 额外跟踪。
type HoldingView = Holding & {
  statusClass: 'win' | 'lose' | 'wait'
  statusLabel: string
  rowClass: string
}
const holdingViews = computed<HoldingView[]>(() =>
  holdings.value.map((h) => {
    if (!h.holding) {
      return { ...h, statusClass: 'wait', statusLabel: '空仓·等买点', rowClass: 'cleared' }
    }
    return h.unrealizedPnl >= 0
      ? { ...h, statusClass: 'win', statusLabel: '持有', rowClass: 'row-win' }
      : { ...h, statusClass: 'lose', statusLabel: '持有·浮亏', rowClass: 'row-lose' }
  }),
)

// 组合整体绩效（19 项指标）。后端 total_performance 现含完整指标，转成
// MetricTable 需要的 Performance 类型（缺失字段补 0 兜底，保证渲染不崩）。
const comboPerf = computed<Performance | null>(() => {
  const tp = store.multiStrategyResult?.total_performance
  if (!tp) return null
  const get = (k: string, d = 0): number => {
    const v = (tp as Record<string, unknown>)[k]
    return typeof v === 'number' ? v : d
  }
  return {
    total_return: get('total_return'),
    annual_return: get('annual_return'),
    max_drawdown: get('max_drawdown'),
    max_dd_duration: get('max_dd_duration'),
    sharpe: get('sharpe'),
    sortino: get('sortino'),
    calmar: get('calmar'),
    total_trades: get('total_trades'),
    win_trades: get('win_trades'),
    lose_trades: get('lose_trades'),
    rejected_trades: get('rejected_trades'),
    win_rate: get('win_rate'),
    profit_factor: get('profit_factor'),
    avg_win: get('avg_win'),
    avg_loss: get('avg_loss'),
    max_win: get('max_win'),
    max_loss: get('max_loss'),
    avg_holding_days: get('avg_holding_days'),
    volatility: get('volatility'),
  }
})

// 组合评级：从 combined_equity 重算夏普/卡玛/回撤/波动率等 5 维度评分，
// 与 /portfolio 页和单标的回测页同口径（复用 gradePortfolio）。
const comboGrade = computed(() =>
  store.multiStrategyResult ? gradePortfolio(store.multiStrategyResult) : null,
)
</script>

<template>
  <div class="strategies-view">
    <header class="page-header">
      <div>
        <h2>策略库</h2>
        <p class="subtitle">
          保存你觉得不错的策略，下次直接载入或重跑。共 {{ strategies.length }} 条。
        </p>
      </div>
      <div class="header-actions">
        <button
          v-if="activeTab === 'single'"
          class="primary sm"
          :disabled="selectedStrategies.length === 0 || store.multiStrategyRunning"
          @click="onComboBacktest"
        >
          {{ store.multiStrategyRunning ? '组合回测中…' : `组合回测（${selectedStrategies.length}）` }}
        </button>
        <button
          v-if="activeTab === 'single' && selectedStrategies.length > 0"
          class="ghost sm"
          @click="clearSelection"
        >
          清除选择
        </button>
        <button class="ghost" :disabled="loading" @click="load">
          {{ loading ? '刷新中…' : '↻ 刷新' }}
        </button>
      </div>
    </header>

    <!-- Tab 切换 -->
    <nav class="tabs" role="tablist">
      <button
        role="tab"
        :aria-selected="activeTab === 'single'"
        :class="['tab', { active: activeTab === 'single' }]"
        @click="switchTab('single')"
      >
        单标的<span class="tab-count">{{ singleStrategies.length }}</span>
      </button>
      <button
        role="tab"
        :aria-selected="activeTab === 'combo'"
        :class="['tab', { active: activeTab === 'combo' }]"
        @click="switchTab('combo')"
      >
        组合<span class="tab-count">{{ comboStrategies.length }}</span>
      </button>
    </nav>

    <div v-if="error || store.error" class="error-banner">⚠ {{ error || store.error }}</div>

    <div v-if="!loading && visibleStrategies.length === 0 && !error" class="placeholder">
      <p>{{ activeTab === 'single' ? '还没有保存的单标的策略。' : '还没有保存的组合策略。' }}</p>
      <p class="hint">
        <template v-if="activeTab === 'single'">
          在「单标的回测」跑出满意结果后，点结果区的「保存策略」即可收藏到这里。
          勾选多个单标的策略还能做「组合回测」。
        </template>
        <template v-else>
          勾选多个单标的策略 → 点「组合回测」→ 跑出结果后点「💾 保存为组合」，
          即可在这里看到。下次点「↻ 重跑到今天」即可获取最新策略信号。
        </template>
      </p>
    </div>

    <div v-if="loading && strategies.length === 0" class="placeholder">
      <p>加载中…</p>
    </div>

    <div v-if="visibleStrategies.length" class="card-grid">
      <article
        v-for="s in visibleStrategies"
        :key="s.id"
        class="card"
        :class="{ selected: selectedIds.has(s.id), 'card-multi': s.kind === 'multi' }"
      >
        <div class="card-head">
          <label
            v-if="s.kind !== 'multi'"
            class="select-box"
            :title="s.context?.symbol ? '加入组合回测' : '组合策略暂不支持组合回测'"
          >
            <input
              type="checkbox"
              :checked="selectedIds.has(s.id)"
              :disabled="!s.context?.symbol"
              @change="toggleSelect(s.id)"
            />
          </label>
          <span v-else class="multi-icon" title="多策略组合">🗂</span>
          <span class="kind-badge" :class="s.kind">
            {{ s.kind === 'multi' ? '多策略' : s.kind === 'portfolio' ? '多标的' : '单标的' }}
          </span>
          <h3 class="card-title">{{ s.name }}</h3>
        </div>

        <div class="card-strategy">
          {{ s.strategy_label || s.strategy }}
          <span class="params">{{ JSON.stringify(s.params) }}</span>
        </div>

        <div class="card-meta">
          <div class="meta-row"><span class="k">标的</span><span class="v">{{ ctxLabel(s) }}</span></div>
          <div class="meta-row"><span class="k">区间</span><span class="v">{{ dateRange(s) }}</span></div>
        </div>

        <div v-if="Object.keys(s.snapshot).length" class="card-snapshot">
          <div class="snap-item">
            <span class="k">总收益</span>
            <span class="v mono" :class="Number(s.snapshot.total_return) > 0 ? 'pos' : 'neg'">
              {{ pct(s.snapshot.total_return) }}
            </span>
          </div>
          <div class="snap-item">
            <span class="k">夏普</span><span class="v mono">{{ num(s.snapshot.sharpe) }}</span>
          </div>
          <div class="snap-item">
            <span class="k">回撤</span><span class="v mono neg">{{ pct(s.snapshot.max_drawdown) }}</span>
          </div>
        </div>

        <div v-if="s.tags.length" class="card-tags">
          <span v-for="t in s.tags" :key="t" class="tag">{{ t }}</span>
        </div>

        <p v-if="s.notes" class="card-notes">{{ s.notes }}</p>

        <div class="card-foot">
          <span class="created">{{ createdShort(s) }}</span>
          <span class="actions">
            <button
              v-if="s.kind === 'multi'"
              class="rerun-btn sm"
              :disabled="store.multiStrategyRunning"
              @click="onLoadMulti(s)"
            >
              {{ store.multiStrategyRunning ? '重跑中…' : '↻ 重跑到今天' }}
            </button>
            <button v-else class="primary sm" @click="onLoad(s)">载入</button>
            <button
              v-if="s.kind !== 'single'"
              class="rename-btn sm"
              :disabled="renameLoading && renameTarget?.id === s.id"
              @click="openRename(s)"
            >
              {{ renameLoading && renameTarget?.id === s.id ? '…' : '改名' }}
            </button>
            <button
              class="danger sm"
              :disabled="deletingId === s.id"
              @click="onDelete(s)"
            >
              {{ deletingId === s.id ? '…' : '删除' }}
            </button>
          </span>
        </div>
      </article>
    </div>

    <!-- 修改组合名称弹窗 -->
    <div v-if="renameOpen" class="modal-mask" @click.self="closeRename">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="rename-modal-title"
        class="modal rename-modal"
        @keydown.esc.prevent="closeRename"
      >
        <h3 id="rename-modal-title">修改组合名称</h3>
        <p class="modal-desc">只修改策略库中的显示名称，不会改变组合配置和历史结果。</p>
        <div class="modal-field">
          <label for="rename-name-input">组合名称</label>
          <input
            id="rename-name-input"
            ref="renameNameRef"
            v-model="renameName"
            maxlength="120"
            placeholder="请输入新的组合名称"
            @keydown.enter.prevent="submitRename"
          />
        </div>
        <p v-if="error" class="modal-error">⚠ {{ error }}</p>
        <div class="modal-actions">
          <button class="ghost sm" :disabled="renameLoading" @click="closeRename">取消</button>
          <button class="primary sm" :disabled="renameLoading" @click="submitRename">
            {{ renameLoading ? '保存中…' : '保存名称' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 保存组合弹窗 -->
    <div
      v-if="saveComboOpen"
      class="modal-mask"
      @click.self="closeSaveCombo"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="combo-modal-title"
        class="modal"
        @keydown.esc.prevent="closeSaveCombo"
      >
        <h3 id="combo-modal-title">保存为策略组合</h3>
        <p class="modal-desc">
          将当前 {{ comboItemsForSave.length }} 个策略的整体配置（策略+参数+标的+资金配比）存为「组合」，
          下次点「↻ 重跑到今天」即可用截至今天的行情算出每个策略的当前信号（持仓/空仓）。
        </p>
        <div class="modal-field">
          <label for="combo-name-input">组合名称</label>
          <input
            id="combo-name-input"
            ref="saveComboNameRef"
            v-model="saveComboName"
            placeholder="如：科技+消费+银行 防守反击组合"
            maxlength="120"
          />
        </div>
        <div class="modal-field">
          <label for="combo-notes-input">备注（可选）</label>
          <textarea
            id="combo-notes-input"
            v-model="saveComboNotes"
            rows="3"
            placeholder="如：牛市跑得好，震荡市待验证"
            maxlength="2000"
          />
        </div>
        <p v-if="error" class="modal-error">⚠ {{ error }}</p>
        <div class="modal-actions">
          <button class="ghost sm" @click="closeSaveCombo">取消</button>
          <button class="primary sm" :disabled="saveComboLoading" @click="submitSaveCombo">
            {{ saveComboLoading ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 多策略组合回测结果（复用组合页图表组件） -->
    <section
      v-if="store.multiStrategyResult || store.multiStrategyRunning"
      ref="comboResultRef"
      class="combo-result"
    >
      <header class="combo-result-header">
        <div>
          <span class="result-eyebrow">PORTFOLIO REPORT</span>
          <h3 class="combo-title">组合回测结果</h3>
          <div v-if="store.multiStrategyResult" class="combo-meta">
            <span>{{ store.multiStrategyResult.total_performance.total_stocks }} 个策略</span>
            <span>总资金 {{ num(store.multiStrategyResult.total_performance.total_cash, 0) }}</span>
            <span>{{ resultStartDate }} — {{ resultEndDate }}</span>
          </div>
        </div>
        <button
          v-if="store.multiStrategyResult && !store.multiStrategyRunning"
          class="save-combo-btn"
          @click="openSaveCombo"
        >
          💾 保存为组合
        </button>
      </header>

      <div v-if="store.multiStrategyRunning && !store.multiStrategyResult" class="combo-loading">
        <span class="loading-dot"></span>
        <strong>正在生成组合报告</strong>
        <small>逐个策略取行情并执行回测，请稍候</small>
      </div>

      <div v-if="store.multiStrategyResult" class="combo-content">
        <!-- 第一层：用户最关心的当前信号 -->
        <section class="signal-hero" :class="`tone-${signalTone}`">
          <div class="signal-copy">
            <span class="section-kicker">当前模型信号 · 截至 {{ resultEndDate }}</span>
            <h4>{{ signalHeadline }}</h4>
            <p>{{ signalDescription }}</p>
          </div>
          <div class="signal-counts" aria-label="当前策略状态汇总">
            <div class="signal-count holding-count">
              <span class="count-value">{{ holdingCount }}</span>
              <span class="count-label">持仓中</span>
            </div>
            <div class="signal-count waiting-count">
              <span class="count-value">{{ waitingCount }}</span>
              <span class="count-label">等买点</span>
            </div>
          </div>
        </section>

        <!-- 第二层：只保留四个核心数字 + 总评级，详细指标收进下方折叠区。 -->
        <div v-if="comboPerf" class="kpi-grid">
          <div class="kpi-card primary-kpi">
            <span class="kpi-label">组合总收益</span>
            <strong class="kpi-value" :class="comboPerf.total_return >= 0 ? 'pos' : 'neg'">
              {{ (comboPerf.total_return * 100).toFixed(2) }}%
            </strong>
            <span class="kpi-note">整个回测区间</span>
          </div>
          <div class="kpi-card">
            <span class="kpi-label">年化收益</span>
            <strong class="kpi-value" :class="comboPerf.annual_return >= 0 ? 'pos' : 'neg'">
              {{ (comboPerf.annual_return * 100).toFixed(2) }}%
            </strong>
            <span class="kpi-note">折算年度表现</span>
          </div>
          <div class="kpi-card risk-kpi">
            <span class="kpi-label">最大回撤</span>
            <strong class="kpi-value">{{ (comboPerf.max_drawdown * 100).toFixed(2) }}%</strong>
            <span class="kpi-note">历史最深亏损幅度</span>
          </div>
          <div class="kpi-card">
            <span class="kpi-label">夏普比率</span>
            <strong class="kpi-value neutral">{{ comboPerf.sharpe.toFixed(2) }}</strong>
            <span class="kpi-note">收益风险性价比</span>
          </div>
          <div v-if="comboGrade" class="kpi-card grade-kpi" :class="`grade-${comboGrade.grade}`">
            <span class="kpi-label">组合评级</span>
            <strong class="kpi-value grade-value">
              {{ comboGrade.grade }} <small>{{ comboGrade.score.toFixed(0) }}分</small>
            </strong>
            <span class="kpi-note">风险调整后体验</span>
          </div>
        </div>

        <!-- 第三层：当前策略逐项信号，放在历史分析之前。 -->
        <section class="report-card priority-card">
          <div class="report-section-head">
            <div>
              <span class="section-kicker">ACTION NOW</span>
              <h4>当前策略信号</h4>
              <p>这是模型在回测结束日的状态，不是你的真实账户持仓</p>
            </div>
            <span class="section-badge">{{ holdingCount }}/{{ holdings.length }} 持仓</span>
          </div>

          <p v-if="holdings.length === 0" class="empty-text">无持仓数据</p>
          <div v-else class="table-scroll">
            <table class="holdings-table signal-table">
              <thead>
                <tr>
                  <th>策略</th>
                  <th>标的</th>
                  <th>当前状态</th>
                  <th class="num">持仓数量</th>
                  <th class="num">成本价</th>
                  <th class="num">市值</th>
                  <th class="num">未实现盈亏</th>
                  <th class="num">收益率</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="h in holdingViews" :key="h.key" :class="h.rowClass">
                  <td class="strategy-name">{{ h.strategyLabel }}</td>
                  <td class="sym">{{ h.symbol }}</td>
                  <td><span class="status-tag" :class="h.statusClass">{{ h.statusLabel }}</span></td>
                  <td class="num">{{ h.size > 0 ? h.size.toFixed(0) : '-' }}</td>
                  <td class="num">{{ h.holding ? h.avgPrice.toFixed(2) : '-' }}</td>
                  <td class="num">{{ h.holding ? h.marketValue.toFixed(0) : '-' }}</td>
                  <td class="num" :class="{ pos: h.unrealizedPnl > 0, neg: h.unrealizedPnl < 0 }">
                    {{ h.holding ? (h.unrealizedPnl > 0 ? '+' : '') + h.unrealizedPnl.toFixed(0) : '-' }}
                  </td>
                  <td class="num" :class="{ pos: h.unrealizedPct > 0, neg: h.unrealizedPct < 0 }">
                    {{ h.holding ? (h.unrealizedPct * 100).toFixed(2) + '%' : '-' }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="model-note">
            模型信号会随新 K 线变化；“持仓”表示尚未触发卖点，“空仓”表示等待下一次买点。
          </div>
        </section>

        <div class="warn-box overfit compact-warning">
          <strong>历史表现不等于未来收益。</strong>
          组合可能只适应特定行情，请把当前状态作为参考信号，并配合自己的仓位和止损纪律。
        </div>

        <!-- 第四层：净值与评级并排，建立收益与风险的直接联系。 -->
        <div class="insight-grid">
          <section class="report-card equity-card">
            <div class="report-section-head compact">
              <div>
                <span class="section-kicker">PERFORMANCE</span>
                <h4>组合净值与回撤</h4>
              </div>
            </div>
            <EquityChart :equity="store.multiStrategyResult.combined_equity" />
          </section>

          <section v-if="comboGrade" class="report-card grade-card">
            <div class="report-section-head compact">
              <div>
                <span class="section-kicker">RISK QUALITY</span>
                <h4>风险评级拆解</h4>
              </div>
            </div>
            <GradeDetails :result="comboGrade" expanded />
          </section>
        </div>

        <section class="report-card">
          <div class="report-section-head compact">
            <div>
              <span class="section-kicker">STRATEGY BREAKDOWN</span>
              <h4>各策略表现</h4>
              <p>比较资金占比、收益、回撤和胜率，识别拖累组合的策略</p>
            </div>
          </div>
          <div class="table-scroll">
            <PortfolioSummaryTable
              :results="store.multiStrategyResult.individual_results"
              :allocation="store.multiStrategyResult.equity_allocation"
            />
          </div>
        </section>

        <details v-if="comboPerf" class="report-card report-disclosure">
          <summary>
            <span>
              <strong>全部绩效指标</strong>
              <small>夏普、卡玛、胜率、盈亏比等 19 项</small>
            </span>
            <span class="disclosure-action">展开查看</span>
          </summary>
          <div class="disclosure-body"><MetricTable :perf="comboPerf" /></div>
        </details>

        <section class="report-card">
          <div class="report-section-head compact">
            <div>
              <span class="section-kicker">RELATIVE VIEW</span>
              <h4>各策略净值对比</h4>
            </div>
          </div>
          <PortfolioCompareChart :results="store.multiStrategyResult.individual_results" />
        </section>

        <!-- 历史成交与买卖点：重跑到今天后仍展示完整回测过程，而不只显示最后持仓。 -->
        <section class="report-card history-trades-block">
          <div class="report-section-head">
            <div>
              <span class="section-kicker">TRADE HISTORY</span>
              <h4>历史买入卖出</h4>
              <p>按策略分别展示成交记录，并在各自 K 线上标出买卖位置</p>
            </div>
            <span class="section-badge muted-badge">{{ totalTradeCount }} 个成交点</span>
          </div>

          <div class="model-note trade-model-note">
            这里是历史模拟成交记录，不是当前下单建议。每套策略独立管理自己的分配资金。
          </div>

          <p v-if="totalTradeCount === 0" class="empty-text">本次回测没有产生买卖记录</p>
          <div v-else class="combined-trades-wrap">
            <table class="holdings-table combined-trades-table">
              <thead>
                <tr>
                  <th>日期</th>
                  <th>策略</th>
                  <th>标的</th>
                  <th>动作</th>
                  <th class="num">数量</th>
                  <th class="num">成交价</th>
                  <th class="num">成交金额</th>
                  <th class="num">平仓盈亏</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in combinedTradeRows" :key="row.key" :class="{ rejected: row.rejected }">
                  <td>{{ tradeDate(row.datetime) }}</td>
                  <td>{{ row.strategy }}</td>
                  <td class="sym">{{ row.symbol }}</td>
                  <td :class="row.direction === 'BUY' ? 'pos' : 'neg'">
                    {{ tradeDirectionLabel(row.direction) }}
                  </td>
                  <td class="num">{{ row.size.toFixed(0) }}</td>
                  <td class="num">{{ row.price.toFixed(3) }}</td>
                  <td class="num">{{ row.amount.toFixed(2) }}</td>
                  <td class="num" :class="{ pos: row.pnl > 0, neg: row.pnl < 0 }">
                    {{ row.pnl === 0 ? '-' : row.pnl.toFixed(2) }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <h4 class="strategy-chart-title">
            分策略 K 线买卖点
            <span class="holdings-hint">展开策略查看 K 线上的买入/卖出标记</span>
          </h4>
          <div v-if="totalTradeCount > 0" class="strategy-trades">
            <details
              v-for="item in strategyTradeResults"
              :key="item.key"
              class="strategy-trade-group"
              :open="expandedTradeKeys.has(item.key)"
              @toggle="onTradeGroupToggle($event, item.key)"
            >
              <summary>
                <span>{{ item.key }}</span>
                <span class="trade-count">{{ item.trades.length }} 笔</span>
              </summary>
              <div v-if="expandedTradeKeys.has(item.key)">
                <div v-if="item.bars.length" class="strategy-kline">
                  <KlineChart :bars="item.bars" :trades="item.trades" :initial-zoom-start="0" />
                </div>
                <p v-else class="empty-text kline-missing">
                  当前任务没有返回 K 线，仅显示成交明细。请重新运行组合回测。
                </p>
                <TradeTable :trades="item.trades" />
              </div>
            </details>
          </div>
        </section>
      </div>
    </section>
  </div>
</template>

<style scoped>
.strategies-view {
  height: 100%;
  overflow-y: auto;
  padding: 16px 20px 32px;
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.page-header h2 {
  font-size: 16px;
  font-weight: 600;
}
.subtitle {
  font-size: 12px;
  color: var(--text-dim);
  margin-top: 4px;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.header-actions .sm {
  font-size: 12px;
  padding: 6px 12px;
  cursor: pointer;
}
.header-actions .primary {
  border-radius: var(--radius);
}

/* Tab 切换条 */
.tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 16px;
}
.tab {
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--text-muted);
  padding: 8px 16px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  border-radius: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  transition: color 0.15s, border-color 0.15s;
}
.tab:hover {
  color: var(--text);
}
.tab.active {
  color: var(--text);
  border-bottom-color: var(--accent);
}
.tab-count {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--border);
  color: var(--text-dim);
  font-weight: 400;
}
.tab.active .tab-count {
  background: rgba(74, 158, 255, 0.18);
  color: var(--accent);
}
.ghost {
  font-size: 12px;
  padding: 6px 12px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text-muted);
  cursor: pointer;
}
.ghost:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
}
.placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  height: 60%;
  color: var(--text-dim);
  gap: 8px;
}
.placeholder .hint {
  font-size: 12px;
  max-width: 420px;
  line-height: 1.6;
}
.error-banner {
  background: rgba(239, 65, 70, 0.12);
  border: 1px solid var(--up);
  color: var(--up);
  padding: 10px 14px;
  border-radius: var(--radius);
  margin-bottom: 16px;
  font-size: 13px;
}

/* 卡片网格 */
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 14px;
}
.card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
/* 勾选框：加入组合回测 */
.select-box {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  cursor: pointer;
}
.select-box input {
  width: 16px;
  height: 16px;
  cursor: pointer;
  accent-color: var(--accent);
}
.select-box input:disabled {
  cursor: not-allowed;
  opacity: 0.3;
}
.card.selected {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent);
}
.kind-badge {
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 4px;
  background: rgba(74, 158, 255, 0.15);
  color: var(--accent);
  flex-shrink: 0;
}
.kind-badge.portfolio {
  background: rgba(140, 110, 220, 0.18);
  color: #b39ddb;
}
.card-title {
  font-size: 14px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-strategy {
  font-size: 13px;
  font-weight: 500;
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}
.card-strategy .params {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-dim);
}
.card-meta {
  font-size: 12px;
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.meta-row {
  display: flex;
  gap: 8px;
}
.meta-row .k {
  color: var(--text-dim);
  width: 32px;
  flex-shrink: 0;
}
.meta-row .v {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-snapshot {
  display: flex;
  gap: 20px;
  padding: 8px 0;
  border-top: 1px dashed var(--border);
  border-bottom: 1px dashed var(--border);
}
.snap-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.snap-item .k {
  font-size: 11px;
  color: var(--text-dim);
}
.snap-item .v {
  font-size: 15px;
  font-weight: 600;
}
.mono {
  font-family: var(--font-mono);
}
.pos {
  color: var(--up);
}
.neg {
  color: var(--down);
}
.card-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  background: var(--border);
  color: var(--text-muted);
}
.card-notes {
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.5;
  white-space: pre-wrap;
}
.card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: auto;
  padding-top: 6px;
}
.created {
  font-size: 11px;
  color: var(--text-dim);
  font-family: var(--font-mono);
}
.actions {
  display: flex;
  gap: 8px;
}
.sm {
  font-size: 12px;
  padding: 4px 12px;
}
.rename-btn {
  border: 1px solid rgba(74, 158, 255, 0.5);
  background: rgba(74, 158, 255, 0.1);
  color: var(--accent);
  border-radius: var(--radius);
  cursor: pointer;
}
.rename-btn:hover:not(:disabled) {
  border-color: var(--accent);
  background: rgba(74, 158, 255, 0.18);
}
.rename-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.danger {
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  border-radius: var(--radius);
  cursor: pointer;
}
.danger:hover:not(:disabled) {
  border-color: var(--up);
  color: var(--up);
}
.danger:disabled {
  opacity: 0.5;
  cursor: default;
}

/* 多策略组合回测结果区 */
.combo-result {
  margin-top: 28px;
  background: linear-gradient(180deg, #141924 0%, #11151e 100%);
  border: 1px solid #303746;
  border-radius: 14px;
  box-shadow: 0 18px 50px rgba(0, 0, 0, 0.28);
  overflow: hidden;
  scroll-margin-top: 16px;
}
.combo-result-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 22px 24px 20px;
  border-bottom: 1px solid rgba(74, 158, 255, 0.18);
  background:
    radial-gradient(circle at 8% 0%, rgba(74, 158, 255, 0.13), transparent 32%),
    rgba(15, 19, 28, 0.76);
}
.result-eyebrow,
.section-kicker {
  display: block;
  color: #6eb4ff;
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1.2px;
  line-height: 1.3;
}
.combo-title {
  margin-top: 4px;
  font-size: 22px;
  font-weight: 750;
  letter-spacing: -0.3px;
}
.combo-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 10px;
}
.combo-meta span {
  padding: 3px 8px;
  border: 1px solid rgba(139, 145, 158, 0.2);
  border-radius: 999px;
  color: var(--text-muted);
  background: rgba(255, 255, 255, 0.025);
  font-family: var(--font-mono);
  font-size: 11px;
}
.combo-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  padding: 54px 24px;
  color: var(--text-muted);
}
.combo-loading strong {
  color: var(--text);
  font-size: 15px;
}
.combo-loading small {
  color: var(--text-dim);
}
.loading-dot {
  width: 10px;
  height: 10px;
  margin-bottom: 5px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 6px rgba(74, 158, 255, 0.12);
  animation: report-pulse 1.2s ease-in-out infinite;
}
@keyframes report-pulse {
  50% { opacity: 0.45; transform: scale(0.8); }
}
.combo-content {
  display: flex;
  flex-direction: column;
  gap: 20px;
  padding: 22px 24px 28px;
}

/* 第一屏：当前结论 */
.signal-hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  min-height: 128px;
  padding: 22px 24px;
  border: 1px solid rgba(74, 158, 255, 0.34);
  border-radius: 12px;
  background: linear-gradient(120deg, rgba(74, 158, 255, 0.14), rgba(74, 158, 255, 0.025));
  box-shadow: inset 4px 0 0 var(--accent);
}
.signal-hero.tone-mixed {
  border-color: rgba(150, 118, 255, 0.34);
  background: linear-gradient(120deg, rgba(132, 96, 220, 0.15), rgba(74, 158, 255, 0.025));
  box-shadow: inset 4px 0 0 #9676ff;
}
.signal-hero.tone-cash {
  border-color: rgba(24, 160, 88, 0.32);
  background: linear-gradient(120deg, rgba(24, 160, 88, 0.13), rgba(24, 160, 88, 0.02));
  box-shadow: inset 4px 0 0 var(--down);
}
.signal-copy h4 {
  margin-top: 8px;
  color: #f1f4f8;
  font-size: 25px;
  font-weight: 750;
  letter-spacing: -0.4px;
}
.signal-copy p {
  margin-top: 5px;
  color: var(--text-muted);
  font-size: 13px;
}
.signal-counts {
  display: flex;
  gap: 10px;
  flex-shrink: 0;
}
.signal-count {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: 92px;
  min-height: 76px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 10px;
  background: rgba(8, 11, 17, 0.42);
}
.count-value {
  font-family: var(--font-mono);
  font-size: 27px;
  font-weight: 750;
  line-height: 1.1;
}
.holding-count .count-value { color: #70b7ff; }
.waiting-count .count-value { color: var(--text-muted); }
.count-label {
  margin-top: 5px;
  color: var(--text-muted);
  font-size: 11px;
}

/* 核心数字 */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(135px, 1fr));
  gap: 10px;
}
.kpi-card {
  position: relative;
  display: flex;
  flex-direction: column;
  min-height: 112px;
  padding: 14px 15px 12px;
  overflow: hidden;
  border: 1px solid #2b3240;
  border-radius: 10px;
  background: rgba(24, 29, 40, 0.84);
}
.kpi-card::before {
  position: absolute;
  top: 0;
  right: 0;
  left: 0;
  height: 2px;
  background: #3b4658;
  content: '';
}
.primary-kpi::before { background: var(--accent); }
.risk-kpi::before { background: var(--warn); }
.grade-kpi::before { background: #9676ff; }
.kpi-label {
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
}
.kpi-value {
  margin-top: 8px;
  font-family: var(--font-mono);
  font-size: 23px;
  font-weight: 750;
  line-height: 1.1;
}
.kpi-value.neutral { color: #d9e6f5; }
.risk-kpi .kpi-value { color: var(--warn); }
.kpi-note {
  margin-top: auto;
  padding-top: 8px;
  color: var(--text-dim);
  font-size: 10px;
}
.grade-value small {
  font-size: 11px;
  font-weight: 600;
  opacity: 0.72;
}
.grade-kpi.grade-S .grade-value { color: #e0b341; }
.grade-kpi.grade-A .grade-value { color: var(--down); }
.grade-kpi.grade-B .grade-value { color: var(--accent); }
.grade-kpi.grade-C .grade-value { color: var(--warn); }
.grade-kpi.grade-D .grade-value { color: var(--up); }

/* 通用报告卡片 */
.report-card {
  padding: 18px 19px;
  border: 1px solid #2a3140;
  border-radius: 11px;
  background: rgba(23, 28, 39, 0.86);
}
.priority-card {
  border-color: rgba(74, 158, 255, 0.3);
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.12);
}
.report-section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 15px;
}
.report-section-head.compact { margin-bottom: 10px; }
.report-section-head h4 {
  margin-top: 3px;
  color: #edf1f7;
  font-size: 16px;
  font-weight: 700;
}
.report-section-head p {
  margin-top: 3px;
  color: var(--text-dim);
  font-size: 11px;
}
.section-badge {
  flex-shrink: 0;
  padding: 4px 10px;
  border: 1px solid rgba(74, 158, 255, 0.35);
  border-radius: 999px;
  color: #82beff;
  background: rgba(74, 158, 255, 0.09);
  font-family: var(--font-mono);
  font-size: 11px;
}
.muted-badge {
  border-color: rgba(139, 145, 158, 0.25);
  color: var(--text-muted);
  background: rgba(139, 145, 158, 0.06);
}
.model-note {
  margin-top: 12px;
  padding: 8px 11px;
  border-radius: 7px;
  color: #7f8998;
  background: rgba(8, 11, 17, 0.34);
  font-size: 11px;
  line-height: 1.55;
}
.table-scroll { overflow-x: auto; }
.insight-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(300px, 0.85fr);
  gap: 14px;
  align-items: stretch;
}
.equity-card,
.grade-card { min-width: 0; }
.equity-card :deep(.equity-chart) { height: 340px; }
.grade-card :deep(.grade-details) { margin-top: 4px; }

/* 次要信息默认折叠，避免 19 项指标抢占第一屏。 */
.report-disclosure { padding: 0; }
.report-disclosure > summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 15px 18px;
  cursor: pointer;
  list-style: none;
}
.report-disclosure > summary::-webkit-details-marker { display: none; }
.report-disclosure > summary strong {
  display: block;
  color: var(--text);
  font-size: 14px;
}
.report-disclosure > summary small {
  display: block;
  margin-top: 2px;
  color: var(--text-dim);
  font-size: 11px;
}
.disclosure-action {
  color: var(--accent);
  font-size: 11px;
}
.report-disclosure[open] .disclosure-action::before { content: '收起 · '; }
.report-disclosure[open] > summary { border-bottom: 1px solid var(--border); }
.disclosure-body { padding: 18px; }

.holdings-hint {
  margin-left: 6px;
  color: var(--text-dim);
  font-size: 11px;
  font-weight: 400;
}
.empty-text {
  padding: 18px 0;
  color: var(--text-dim);
  font-size: 13px;
  text-align: center;
}

/* 历史交易 */
.strategy-trades {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.history-trades-block { scroll-margin-top: 18px; }
.trade-model-note { margin: 0 0 12px; }
.combined-trades-wrap {
  max-height: 440px;
  overflow: auto;
  border: 1px solid #2b3240;
  border-radius: 8px;
}
.combined-trades-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: #171c27;
}
.combined-trades-table tr.rejected td {
  opacity: 0.45;
  text-decoration: line-through;
}
.strategy-chart-title {
  margin: 20px 0 10px;
  color: var(--text-muted);
  font-size: 13px;
}
.kline-missing {
  padding: 16px 12px;
  border-top: 1px solid var(--border);
}
.strategy-trade-group {
  overflow: hidden;
  border: 1px solid #2b3240;
  border-radius: 8px;
}
.strategy-trade-group summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 13px;
  color: #c4cad3;
  background: rgba(11, 14, 21, 0.28);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}
.strategy-trade-group summary:hover { background: rgba(74, 158, 255, 0.06); }
.strategy-trade-group .trade-count {
  flex: none;
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 400;
}
.strategy-kline {
  padding: 10px 6px 0;
  border-top: 1px solid var(--border);
}
.strategy-trade-group :deep(.trade-table-wrap) {
  max-height: 360px;
  overflow: auto;
  border-top: 1px solid var(--border);
}

/* 表格强调状态而不是所有数字一起抢眼。 */
.holdings-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  font-size: 12px;
}
.holdings-table th,
.holdings-table td {
  padding: 10px 11px;
  border-bottom: 1px solid rgba(42, 46, 58, 0.72);
  text-align: left;
  white-space: nowrap;
}
.holdings-table th {
  color: #717a88;
  background: rgba(10, 13, 19, 0.28);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.3px;
}
.holdings-table tbody tr { transition: background 0.15s ease; }
.holdings-table tbody tr:hover { background: rgba(74, 158, 255, 0.045); }
.holdings-table .num {
  text-align: right;
  font-family: var(--font-mono);
}
.holdings-table .sym {
  color: #a9cbed;
  font-family: var(--font-mono);
  font-weight: 600;
}
.strategy-name {
  color: #dfe4ea;
  font-weight: 600;
}
.holdings-table tr.cleared { opacity: 0.54; }
.status-tag {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 3px 9px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 700;
}
.status-tag.win {
  color: #ff777b;
  background: rgba(239, 65, 70, 0.12);
  border: 1px solid rgba(239, 65, 70, 0.26);
}
.status-tag.lose {
  color: #f5b758;
  background: rgba(240, 160, 32, 0.12);
  border: 1px solid rgba(240, 160, 32, 0.25);
}
.status-tag.wait {
  color: #8893a2;
  background: rgba(139, 145, 158, 0.09);
  border: 1px solid rgba(139, 145, 158, 0.2);
}
.holdings-table tr.row-win { background: rgba(239, 65, 70, 0.025); }
.holdings-table tr.row-lose { background: rgba(240, 160, 32, 0.025); }

@media (max-width: 1100px) {
  .kpi-grid { grid-template-columns: repeat(3, minmax(140px, 1fr)); }
  .insight-grid { grid-template-columns: 1fr; }
}
@media (max-width: 720px) {
  .combo-result-header,
  .signal-hero { flex-direction: column; }
  .combo-result-header { padding: 18px; }
  .combo-content { padding: 16px; }
  .signal-hero { align-items: stretch; padding: 18px; }
  .signal-counts { width: 100%; }
  .signal-count { flex: 1; width: auto; }
  .kpi-grid { grid-template-columns: repeat(2, minmax(125px, 1fr)); }
  .report-card { padding: 15px; }
}

/* 组合卡片 / multi 视觉差异 */
.card-multi {
  border-color: rgba(245, 158, 11, 0.35);
  background: linear-gradient(180deg, rgba(245, 158, 11, 0.04), var(--bg-panel) 30%);
}
.card-multi:hover {
  border-color: rgba(245, 158, 11, 0.6);
}
.multi-icon {
  font-size: 18px;
  color: #f59e0b;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  width: 16px;
  text-align: center;
}
.kind-badge.multi {
  background: rgba(245, 158, 11, 0.18);
  color: #f59e0b;
}

/* 保存组合按钮（结果区右上） */
.save-combo-btn {
  font-size: 12px;
  flex-shrink: 0;
  padding: 7px 13px;
  margin: 0;
  background: linear-gradient(135deg, #f59e0b, #ea580c);
  border: 1px solid #f59e0b;
  color: #fff;
  font-weight: 600;
  border-radius: var(--radius);
  cursor: pointer;
  vertical-align: middle;
}
.save-combo-btn:hover {
  background: linear-gradient(135deg, #fbbf24, #f59e0b);
}

/* 警示条基础类（过拟合 / 免责共享） */
.warn-box {
  padding: 10px 14px;
  border-radius: var(--radius);
  margin-bottom: 12px;
  font-size: 12px;
  line-height: 1.6;
}
.warn-box strong {
  color: #fbbf24;
}
.warn-box.overfit {
  background: rgba(240, 160, 32, 0.1);
  border-left: 3px solid var(--warn);
  color: #f0a020;
  margin-bottom: 14px;
}
.compact-warning {
  margin: 0;
  border: 1px solid rgba(240, 160, 32, 0.2);
  border-left: 3px solid var(--warn);
}
.warn-box.disclaimer {
  background: rgba(240, 160, 32, 0.06);
  border: 1px dashed rgba(240, 160, 32, 0.4);
  color: var(--text-muted);
  font-size: 11px;
  padding: 8px 12px;
}

/* 多策略组合卡片的"重跑到今天"按钮：橙色 outline，呼应组合主题色 */
.rerun-btn {
  background: rgba(245, 158, 11, 0.12);
  border: 1px solid rgba(245, 158, 11, 0.5);
  color: #f59e0b;
  font-weight: 600;
  border-radius: var(--radius);
  cursor: pointer;
}
.rerun-btn:hover:not(:disabled) {
  background: rgba(245, 158, 11, 0.2);
  border-color: #f59e0b;
  color: #fbbf24;
}
.rerun-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 保存组合弹窗 */
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.modal {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 20px 22px;
  width: 420px;
  max-width: calc(100vw - 32px);
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.5);
}
.modal h3 {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 6px;
}
.modal-desc {
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.6;
  margin-bottom: 14px;
}
.modal-field {
  margin-bottom: 12px;
}
.modal-field textarea {
  resize: vertical;
  font-family: inherit;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}
.modal-error {
  color: var(--up);
  font-size: 12px;
  line-height: 1.5;
  margin: 4px 0 0;
}
</style>
