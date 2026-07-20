<script setup lang="ts">
// 选标的 + 配置日期范围（取行情由父组件在「开始回测/开始寻优」时触发）。
// 市场按 6 位代码智能识别，不再手动选择。
// 后端 /bars 仅支持 count（上限 800，约 3.2 年），固定拉满后前端按日期过滤。
// 默认：结束日=今天（最近交易日），开始日=2020-01-06。

import { computed, ref } from 'vue'

import { fetchBars, fetchExBars, fetchSecurityName, formatError } from '../api'
import { detectMarket, detectExMarket, exMarketName, isExMarketCode, marketLabel } from '../market'
import { useBacktestStore } from '../stores/backtest'
import type { Bar, Category } from '../types'

const store = useBacktestStore()

// 代码 / 周期 / 日期通过 defineModel 与父组件双向同步：
// 既允许父组件读取（如寻优页「查看」按钮拼 URL 带上这些值），
// 也允许父组件写入（如回测页从 URL query 回填表单）。
// 未绑定时取默认值，向后兼容。
//
// 注意：defineModel 的 default 不能引用本 <script setup> 内声明的局部函数
// （编译期会被 hoist 到 setup() 外，此时函数还未定义），
// 因此日期默认值用内联字面量表达式计算。
const code = defineModel<string>('code', { default: '000001' })
const category = defineModel<Category>('category', { default: 'DAY' })
const startDate = defineModel<string>('startDate', {
  default: '2020-01-06',
})
const endDate = defineModel<string>('endDate', {
  default: new Date().toISOString().slice(0, 10),
})

const error = ref('')
// loading 由父组件控制（回测/寻优时驱动），组件自身只暴露 loadBars
const loading = ref(false)
// 当前标的名称（A 股走 /quotes 实时接口；美股/港股走本地常用代码映射）
const symbolName = ref('')

const CATEGORIES: Category[] = ['DAY', 'WEEK', 'MONTH', 'MIN_5', 'MIN_15', 'MIN_30', 'MIN_60']

// 智能识别的市场（用于提示展示）
const detectedMarket = computed(() => {
  const c = code.value?.trim()
  if (!c) return ''
  if (isExMarketCode(c)) return marketLabel(detectExMarket(c))
  if (/^\d{6}$/.test(c)) return marketLabel(detectMarket(c))
  return ''
})

/** 取行情（由父组件在点击「开始回测/开始寻优」时调用）。
 * 成功返回 true，失败返回 false（并把错误写入 store.error 供父组件感知）。 */
async function loadBars(): Promise<boolean> {
  const c = code.value.trim()
  // 基本校验：6位数字(A股) 或 1-5位字母(美股) 或 5位数字(港股)
  if (!/^\d{6}$/.test(c) && !/^[A-Za-z]{1,5}$/.test(c) && !/^\d{5}$/.test(c)) {
    error.value = '代码格式无效：A股为6位数字，美股为1-5位字母（如SPY），港股为5位数字'
    store.error = error.value
    return false
  }
  if (startDate.value >= endDate.value) {
    error.value = '开始日期必须早于结束日期'
    store.error = error.value
    return false
  }

  loading.value = true
  error.value = ''
  try {
    let bars: Bar[]
    let sourceLabel: string
    const range = `${startDate.value} ~ ${endDate.value}`

    if (isExMarketCode(c)) {
      // 美股/港股走扩展市场接口
      const exMarket = detectExMarket(c)
      bars = await fetchExBars(
        exMarket,
        c.toUpperCase(),
        category.value,
        startDate.value,
        endDate.value,
      )
      sourceLabel = `${exMarket}:${c.toUpperCase()} ${category.value} ${range}`
    } else {
      // A股走标准接口
      const market = detectMarket(c)
      bars = await fetchBars(
        market,
        c,
        category.value,
        startDate.value,
        endDate.value,
      )
      sourceLabel = `${market}:${c} ${category.value} ${range}`
    }

    if (bars.length < 2) {
      error.value = `该日期范围内仅取到 ${bars.length} 根 K 线，不足以回测`
      store.error = error.value
      return false
    }
    store.setOhlcv(bars, sourceLabel)
    store.clearResult()

    // 异步取标的名称（不阻塞回测主流程）
    if (isExMarketCode(c)) {
      symbolName.value = exMarketName(c)
    } else {
      symbolName.value = '' // 先清空，避免显示上一个标的
      fetchSecurityName(detectMarket(c), c).then((n) => {
        // 防止竞态：用户已切到别的标的时不要覆盖
        if (code.value.trim() === c) symbolName.value = n
      })
    }

    return true
  } catch (e) {
    error.value = formatError(e)
    store.error = error.value
    return false
  } finally {
    loading.value = false
  }
}

// 暴露给父组件（BacktestView / OptimizeView）在「开始回测/寻优」时串联调用
defineExpose({ loadBars, loading, symbolName })
</script>

<template>
  <div class="symbol-picker">
    <div class="field code-field">
      <label>代码</label>
      <input
        v-model="code"
        maxlength="10"
        placeholder="A股6位数字 / 美股字母(如SPY) / 港股5位数字"
      />
      <span v-if="detectedMarket" class="market-tag">{{ detectedMarket }}</span>
    </div>

    <div class="field">
      <label>周期</label>
      <select v-model="category">
        <option v-for="c in CATEGORIES" :key="c" :value="c">{{ c }}</option>
      </select>
    </div>

    <div class="row">
      <div class="field">
        <label>开始日期</label>
        <input v-model="startDate" type="date" />
      </div>
      <div class="field">
        <label>结束日期</label>
        <input v-model="endDate" type="date" />
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <p v-if="store.barsSource" class="ok">
      已加载：{{ store.barsSource }}（{{ store.ohlcv.length }} 根）
      <span v-if="symbolName" class="symbol-name"> · {{ symbolName }}</span>
    </p>
  </div>
</template>

<style scoped>
.code-field {
  position: relative;
}
.code-field input {
  padding-right: 70px;
}
.market-tag {
  position: absolute;
  right: 8px;
  bottom: 8px;
  font-size: 11px;
  color: var(--text-dim);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  padding: 1px 6px;
  border-radius: 3px;
}
.err {
  color: var(--up);
  font-size: 12px;
  margin-top: 8px;
}
.ok {
  color: var(--down);
  font-size: 12px;
  margin-top: 8px;
}
.symbol-name {
  color: var(--text-muted);
  font-weight: 600;
}
</style>
