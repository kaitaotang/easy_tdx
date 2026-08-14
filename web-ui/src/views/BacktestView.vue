<script setup lang="ts">
// 回测主页面：左配置面板 / 右报告面板。
// 编排：点击「开始回测」→ 自动取行情 → 回测 → 展示 K线+净值+指标+成交。
// 取行情已整合进「开始回测」（不再有单独的取行情按钮）。

import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import EquityChart from '../components/EquityChart.vue'
import DividendProfile from '../components/DividendProfile.vue'
import GradeDetails from '../components/GradeDetails.vue'
import KlineChart from '../components/KlineChart.vue'
import MetricTable from '../components/MetricTable.vue'
import StrategyPicker from '../components/StrategyPicker.vue'
import SymbolPicker from '../components/SymbolPicker.vue'
import TradeTable from '../components/TradeTable.vue'
import {
  fetchDividendEvents,
  fetchCurrentDividendYield,
  fetchIndexBars,
  fetchSavedStrategy,
  formatError,
  saveStrategy,
  updateSavedStrategy,
} from '../api'
import { detectIndexMarket, detectMarket, isExMarketCode, isIndexCode, toFullSymbol } from '../market'
import { gradePerformance } from '../grading'
import type { Category, ExecutionMode, SavedStrategyCreate } from '../types'
import { useBacktestStore } from '../stores/backtest'

const store = useBacktestStore()
const route = useRoute()

// SymbolPicker 实例引用，用于触发取行情
const symbolPicker = ref<InstanceType<typeof SymbolPicker> | null>(null)

// 镜像 SymbolPicker 的代码/周期/日期，与 SymbolPicker 通过 v-model 双向同步。
// 初始值与 SymbolPicker 默认一致；onMounted 时若 URL query 带了寻优页传来的值则覆盖。
const code = ref('000001')
const category = ref<Category>('DAY')
function isoDaysFromNow(days: number): string {
  const d = new Date()
  d.setDate(d.getDate() + days)
  const year = d.getFullYear()
  const month = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}
const startDate = ref('2020-01-06')
const endDate = ref(isoDaysFromNow(0))

// 表单状态（v-model 给子组件）
const strategy = ref('ma_cross')
const params = ref<Record<string, number | string | boolean>>({})
const cash = ref(1000000)
const commission = ref(0.0003)
const slippage = ref(0)
const execution = ref<ExecutionMode>('next_open')
const dividendUnavailableReason = ref('')
// 从策略库载入时保存原记录 id；后续保存将 PATCH 覆盖这条记录。
const loadedStrategyId = ref<string | null>(null)
const loadedStrategyName = ref('')
const loadedStrategyTags = ref<string[]>([])
const loadedStrategyNotes = ref('')
const loadedRecordReady = ref(false)

// 成交价模式（精简为 开盘价/收盘价）
const EXECUTIONS: { value: ExecutionMode; label: string }[] = [
  { value: 'next_open', label: '开盘价' },
  { value: 'next_close', label: '收盘价' },
]

onMounted(async () => {
  await store.loadStrategies().catch((e) => {
    store.error = `加载策略列表失败：${e instanceof Error ? e.message : e}`
  })

  // 从 URL query 读取寻优页传来的 strategy + params（跳转自动填充）
  const qStrategy = route.query.strategy as string | undefined
  const qParams = route.query.params as string | undefined
  if (qStrategy) {
    strategy.value = qStrategy
    // 等待 StrategyPicker 的 watch(selectedSchema) 触发完默认值重置后，
    // 再用 query 的 params 覆盖，避免被 watch 重置掉
    await nextTick()
  }
  if (qParams) {
    try {
      params.value = JSON.parse(qParams) as Record<string, number | string | boolean>
    } catch {
      // query 参数解析失败，忽略
    }
  }

  // 从 URL query 回填标的代码 / 周期 / 日期范围（寻优页「查看」跳转带来）。
  // 各字段独立 if 守卫：老书签（只有 strategy/params）仍保持默认值，向后兼容。
  const qSymbol = route.query.symbol as string | undefined
  const qStartDate = route.query.startDate as string | undefined
  const qEndDate = route.query.endDate as string | undefined
  const qCategory = route.query.category as Category | undefined
  const qSavedId = route.query.savedId as string | undefined
  const qSavedName = route.query.savedName as string | undefined
  const qAutoRun = route.query.autoRun as string | undefined
  if (qSymbol) code.value = qSymbol
  if (qStartDate) startDate.value = qStartDate
  if (qEndDate) endDate.value = qEndDate
  if (qCategory) category.value = qCategory
  if (qSavedId) loadedStrategyId.value = qSavedId
  if (qSavedName) loadedStrategyName.value = qSavedName

  // 用原记录补齐名称、标签、备注和交易成本配置。保存时默认保留这些内容，
  // 避免一次“重跑并更新”意外把用户原来的备注/标签清空。
  if (qSavedId) {
    try {
      const saved = await fetchSavedStrategy(qSavedId)
      loadedStrategyName.value = saved.name
      loadedStrategyTags.value = saved.tags
      loadedStrategyNotes.value = saved.notes
      loadedRecordReady.value = true
      const config = saved.trade_config || {}
      const savedCash = Number(config.cash)
      const savedCommission = Number(config.commission)
      const savedSlippage = Number(config.slippage)
      if (Number.isFinite(savedCash) && savedCash > 0) cash.value = savedCash
      if (Number.isFinite(savedCommission) && savedCommission >= 0) {
        commission.value = savedCommission
      }
      if (Number.isFinite(savedSlippage) && savedSlippage >= 0) slippage.value = savedSlippage
      if (config.execution === 'next_open' || config.execution === 'next_close') {
        execution.value = config.execution
      }
    } catch (e) {
      store.error = `载入策略库记录失败：${formatError(e)}`
    }
  }

  // 策略库“载入”带 autoRun=1：行情参数回填完成后自动取行情并重跑。
  if (qAutoRun === '1') {
    await nextTick()
    const succeeded = await onRun()
    if (succeeded && loadedRecordReady.value) {
      await updateLoadedStrategyAfterAutoRun()
    } else if (succeeded && qSavedId) {
      store.error = '回测已完成，但原策略记录未成功载入，因此没有自动覆盖旧记录。'
    }
  }
})

// 取行情 + 回测 串联（点击「开始回测」触发）
async function onRun(): Promise<boolean> {
  // 先清掉旧结果，避免取行情失败时误把上一次结果写回策略库。
  store.clearResult()
  dividendUnavailableReason.value = ''
  // 1. 先取行情（SymbolPicker.loadBars 会校验并填充 store.ohlcv）
  const ok = await symbolPicker.value?.loadBars()
  if (!ok) return false // 校验/取数失败，错误已在 store.error
  // A 股有 TDX 除权除息历史；扩展市场暂无同口径历史数据，清空避免串标的。
  let dividends = undefined
  let dividendBars = undefined
  let dividendSource = undefined
  let dividendYieldPct = undefined
  if ((!isExMarketCode(code.value) || isIndexCode(code.value)) && category.value === 'DAY') {
    const baseCode = code.value.trim()
    const baseMarket = isIndexCode(baseCode) ? detectIndexMarket(baseCode) : detectMarket(baseCode)
    try {
      dividends = await fetchDividendEvents(baseMarket, baseCode)
    } catch (e) {
      dividends = undefined
      dividendUnavailableReason.value = `股息数据读取失败：${formatError(e)}`
    }
    const hasCash = (dividends || []).some(
      (event) => Number(event.fenhong) > 0 && (event.category == null || event.category === 1),
    )
    // 512890 等 ETF 的自身 XDXR 可能为空，回退到其跟踪的中证指数 H30269。
    if (!hasCash && baseCode === '512890') {
      const indexCode = 'H30269'
      const indexMarket = detectIndexMarket(indexCode)
      try {
        const [indexBars, indexDividends] = await Promise.all([
          fetchIndexBars(indexMarket, indexCode, category.value, startDate.value, endDate.value),
          fetchDividendEvents(indexMarket, indexCode),
        ])
        const indexHasCash = indexDividends.some(
          (event) => Number(event.fenhong) > 0 && (event.category == null || event.category === 1),
        )
        if (indexBars.length >= 2 && indexHasCash) {
          dividendBars = indexBars
          dividends = indexDividends
          dividendSource = `${indexMarket}:${indexCode}（512890跟踪指数，参考股息）`
        }
      } catch {
        // 指数分红接口不可用时继续尝试当前行情源股息率。
      }
      if (!dividendSource) {
        try {
          const currentYield = await fetchCurrentDividendYield('CSI_INDEX', indexCode)
          if (currentYield != null) {
            dividendYieldPct = currentYield
            dividendSource = `CSI_INDEX:${indexCode}（512890跟踪指数，行情源当前参考股息）`
          } else {
            dividendUnavailableReason.value = `${indexCode} 的行情源股息率字段不可用（返回0或缺失），不能据此认定股息率为0%。`
          }
        } catch (e) {
          // MAC 行情源不可用时保持无股息率展示，不影响回测。
          dividendUnavailableReason.value = `${indexCode} 的参考股息率读取失败：${formatError(e)}`
        }
      }
    }
    if (isIndexCode(baseCode) && hasCash) {
      dividendSource = `${baseMarket}:${baseCode}（指数自身股息）`
    }
    if (isIndexCode(baseCode) && !hasCash) {
      try {
        const currentYield = await fetchCurrentDividendYield('CSI_INDEX', baseCode)
        if (currentYield != null) {
          dividendYieldPct = currentYield
          dividendSource = `CSI_INDEX:${baseCode}（行情源当前参考股息）`
        } else {
          dividendUnavailableReason.value = `${baseCode} 的行情源股息率字段不可用（返回0或缺失），不能据此认定股息率为0%。`
        }
      } catch (e) {
        // 当前股息率是辅助指标，失败不阻断回测。
        dividendUnavailableReason.value = `${baseCode} 的参考股息率读取失败：${formatError(e)}`
      }
    }
  } else if (category.value !== 'DAY') {
    dividendUnavailableReason.value = '历史股息率目前按日线收盘价计算，请把周期切换为 DAY。'
  } else {
    dividendUnavailableReason.value = '当前数据源暂未提供美股或港股的历史现金分红。'
  }
  // 2. 再回测
  await store.run({
    strategy: strategy.value,
    params: params.value,
    cash: cash.value,
    commission: commission.value,
    slippage: slippage.value,
    execution: execution.value,
    dividends,
    dividend_bars: dividendBars,
    dividend_source: dividendSource,
    dividend_yield_pct: dividendYieldPct,
  })
  return store.result !== null
}

// ── 保存策略（把当前结果 + 配置 + 上下文存进策略库）──────────────────────────
const showSaveForm = ref(false)
const saving = ref(false)
const saveName = ref('')
const saveTags = ref('')
const saveNotes = ref('')
const saveMsg = ref('') // 保存后提示（成功/失败）

const strategyLabel = computed(
  () => store.strategies.find((s) => s.name === strategy.value)?.label ?? strategy.value,
)

// 评级：基于完整 Performance，6 维度评分 + 一票否决。
// total_return 不直接计入评分（只通过卡玛/夏普间接体现），
// 体现「哪怕近期收益率高，长期风险大也该低评」的产品诉求。
const grade = computed(() =>
  store.result ? gradePerformance(store.result.performance) : null,
)

function openSaveForm() {
  saveName.value = loadedStrategyName.value || `${strategyLabel.value} · ${code.value}`
  saveTags.value = loadedStrategyTags.value.join(',')
  saveNotes.value = loadedStrategyNotes.value
  saveMsg.value = ''
  showSaveForm.value = true
}

function buildSavedPayload(
  name: string,
  tags: string[],
  notes: string,
): SavedStrategyCreate {
  if (!store.result) throw new Error('没有可保存的回测结果')
  return {
    name,
    kind: 'single',
    strategy: strategy.value,
    strategy_label: strategyLabel.value,
    params: params.value,
    context: {
      symbol: toFullSymbol(code.value),
      category: category.value,
      start_date: startDate.value,
      end_date: endDate.value,
    },
    trade_config: {
      cash: cash.value,
      commission: commission.value,
      min_commission: 5,
      stamp_tax: 0.001,
      slippage: slippage.value,
      execution: execution.value,
    },
    snapshot: {
      total_return: store.result.performance.total_return,
      annual_return: store.result.performance.annual_return,
      max_drawdown: store.result.performance.max_drawdown,
      sharpe: store.result.performance.sharpe,
      win_rate: store.result.performance.win_rate,
      trades_count: store.result.performance.total_trades,
    },
    tags,
    notes,
  }
}

function applySavedRecord(saved: Awaited<ReturnType<typeof updateSavedStrategy>>) {
  loadedStrategyId.value = saved.id
  loadedStrategyName.value = saved.name
  loadedStrategyTags.value = saved.tags
  loadedStrategyNotes.value = saved.notes
  loadedRecordReady.value = true
}

/** 策略库载入后的自动重跑成功时，立即用今天的结果覆盖原记录。 */
async function updateLoadedStrategyAfterAutoRun() {
  const id = loadedStrategyId.value
  if (!id || !store.result) return
  const name = loadedStrategyName.value || `${strategyLabel.value} · ${code.value}`
  try {
    const payload = buildSavedPayload(
      name,
      loadedStrategyTags.value,
      loadedStrategyNotes.value,
    )
    const saved = await updateSavedStrategy(id, payload)
    applySavedRecord(saved)
    saveMsg.value = `✓ 已重跑到 ${endDate.value} 并更新策略库`
  } catch (e) {
    store.error = `回测已完成，但更新策略库失败：${formatError(e)}`
  }
}

async function onSave() {
  if (!store.result || !saveName.value.trim()) return
  saving.value = true
  saveMsg.value = ''
  try {
    const wasUpdate = Boolean(loadedStrategyId.value)
    const tags = saveTags.value
      .split(/[,，]/)
      .map((t) => t.trim())
      .filter(Boolean)
    const payload = buildSavedPayload(saveName.value.trim(), tags, saveNotes.value)
    const saved = loadedStrategyId.value
      ? await updateSavedStrategy(loadedStrategyId.value, payload)
      : await saveStrategy(payload)
    applySavedRecord(saved)
    saveMsg.value = wasUpdate ? '✓ 已更新策略库' : '✓ 已保存到策略库'
    showSaveForm.value = false
  } catch (e) {
    saveMsg.value = `保存失败：${formatError(e)}`
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="backtest-view">
    <!-- 左栏：配置 -->
    <aside class="config-panel">
      <section class="panel-section">
        <h3>行情数据</h3>
        <SymbolPicker
          ref="symbolPicker"
          v-model:code="code"
          v-model:category="category"
          v-model:start-date="startDate"
          v-model:end-date="endDate"
        />
      </section>

      <section class="panel-section">
        <h3>策略</h3>
        <StrategyPicker
          v-if="store.strategies.length"
          :strategies="store.strategies"
          v-model:strategy="strategy"
          v-model:params="params"
        />
        <p v-else class="loading-text">加载策略中…</p>
      </section>

      <section class="panel-section">
        <h3>资金与成本</h3>
        <div class="field">
          <label>初始资金</label>
          <input v-model.number="cash" type="number" min="1000" step="10000" />
        </div>
        <div class="row">
          <div class="field">
            <label>佣金率</label>
            <input v-model.number="commission" type="number" min="0" step="0.0001" />
          </div>
          <div class="field">
            <label>滑点</label>
            <input v-model.number="slippage" type="number" min="0" step="0.001" />
          </div>
        </div>
        <div class="field">
          <label>成交价</label>
          <select v-model="execution">
            <option v-for="e in EXECUTIONS" :key="e.value" :value="e.value">{{ e.label }}</option>
          </select>
        </div>
      </section>

      <button
        class="primary run-btn"
        :disabled="store.running"
        @click="onRun"
      >
        {{ store.running ? '取行情+回测中…' : '开始回测' }}
      </button>
    </aside>

    <!-- 右栏：报告 -->
    <main class="report-panel">
      <div v-if="store.error" class="error-banner">⚠ {{ store.error }}</div>

      <div v-if="!store.result && !store.running && !store.error" class="placeholder">
        <p>输入代码、配置策略后点击「开始回测」（自动取行情）</p>
      </div>

      <div v-if="store.result" class="report-content">
        <div class="result-toolbar">
          <button class="ghost" @click="openSaveForm">
            💾 {{ loadedStrategyId ? '更新已保存策略' : '保存策略' }}
          </button>
          <span v-if="saveMsg" class="save-msg">{{ saveMsg }}</span>
        </div>

        <section class="report-section">
          <h3>K线 + 买卖点</h3>
          <KlineChart :bars="store.ohlcv" :trades="store.result.trades" />
        </section>

        <section class="report-section">
          <h3>净值曲线与回撤</h3>
          <EquityChart :equity="store.result.equity_curve" />
        </section>

        <section class="report-section">
          <h3>股息率</h3>
          <DividendProfile
            :profile="store.result.dividend_profile"
            :unavailable-reason="dividendUnavailableReason"
          />
        </section>

        <section v-if="grade" class="report-section">
          <h3>评级</h3>
          <GradeDetails :result="grade" expanded />
        </section>

        <section class="report-section">
          <h3>绩效指标</h3>
          <MetricTable :perf="store.result.performance" />
        </section>

        <section class="report-section">
          <h3>成交记录（{{ store.result.trades.length }} 笔）</h3>
          <TradeTable :trades="store.result.trades" />
        </section>
      </div>
    </main>

    <!-- 保存策略对话框 -->
    <div v-if="showSaveForm" class="modal-overlay" @click.self="showSaveForm = false">
      <div class="modal">
        <h3>{{ loadedStrategyId ? '更新策略库记录' : '保存到策略库' }}</h3>
        <p class="modal-desc">
          将当前策略 + 标的上下文 + 成绩快照{{ loadedStrategyId ? '更新到原记录' : '存下' }}，下次可在「策略库」载入或重跑。
        </p>
        <div class="field">
          <label>名称</label>
          <input v-model="saveName" type="text" placeholder="给这个策略起个名" />
        </div>
        <div class="field">
          <label>标签（逗号分隔，可选）</label>
          <input v-model="saveTags" type="text" placeholder="如：银行,长线观察" />
        </div>
        <div class="field">
          <label>备注（可选）</label>
          <textarea v-model="saveNotes" rows="2" placeholder="为什么觉得它好？"></textarea>
        </div>
        <div class="modal-summary">
          {{ strategyLabel }} · {{ code }} ·
          {{ store.result ? (store.result.performance.total_return * 100).toFixed(2) + '%' : '' }}
        </div>
        <div class="modal-actions">
          <button class="ghost" :disabled="saving" @click="showSaveForm = false">取消</button>
          <button class="primary" :disabled="saving || !saveName.trim()" @click="onSave">
            {{ saving ? '保存中…' : loadedStrategyId ? '更新' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.backtest-view {
  display: flex;
  height: 100%;
}

/* 左栏配置面板 */
.config-panel {
  width: 320px;
  flex-shrink: 0;
  background: var(--bg-panel);
  border-right: 1px solid var(--border);
  padding: 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}
.panel-section {
  margin-bottom: 20px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--border);
}
.panel-section:last-of-type {
  border-bottom: none;
}
.panel-section h3 {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 12px;
}
.loading-text {
  color: var(--text-dim);
  font-size: 12px;
}
.run-btn {
  margin-top: auto;
  width: 100%;
  padding: 10px;
  font-size: 14px;
}

/* 右栏报告面板 */
.report-panel {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
}
.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-dim);
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
.report-section {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px 16px;
  margin-bottom: 16px;
}
.report-section h3 {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-muted);
  margin-bottom: 12px;
}

/* 结果工具条 + 保存对话框 */
.result-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.result-toolbar .ghost {
  font-size: 12px;
  padding: 6px 12px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text-muted);
  cursor: pointer;
}
.result-toolbar .ghost:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.save-msg {
  font-size: 12px;
  color: var(--up);
}
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.modal {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 20px;
  width: 380px;
  max-width: 90vw;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.modal h3 {
  font-size: 15px;
  font-weight: 600;
}
.modal-desc {
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.5;
}
.modal .field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.modal .field label {
  font-size: 12px;
  color: var(--text-muted);
}
.modal .field input,
.modal .field textarea {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 7px 9px;
  font-size: 13px;
  color: var(--text);
  font-family: inherit;
  resize: vertical;
}
.modal .field textarea {
  font-family: inherit;
}
.modal-summary {
  font-size: 12px;
  color: var(--text-dim);
  font-family: var(--font-mono);
  padding: 8px 10px;
  background: var(--bg);
  border-radius: var(--radius);
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 4px;
}
.modal-actions .ghost {
  font-size: 13px;
  padding: 7px 16px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text-muted);
  cursor: pointer;
}
.modal-actions .primary {
  font-size: 13px;
  padding: 7px 16px;
  cursor: pointer;
}
.modal-actions .primary:disabled,
.modal-actions .ghost:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
