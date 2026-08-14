<script setup lang="ts">
// 单标回测的股息率摘要：近12个月滚动股息率、当前历史分位和历史曲线。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import echarts from '../echarts-setup'
import type { DividendProfile as DividendProfileData } from '../types'

const props = defineProps<{
  profile: DividendProfileData | null | undefined
  unavailableReason?: string
}>()
const container = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null

const referenceOnly = computed(() => props.profile?.reference_only === true)
const indexDerived = computed(() => {
  const source = props.profile?.source || ''
  return source.includes('指数') || source.includes('CSI_INDEX') || source.includes('H30269')
})

function formatValue(value: number | null | undefined, suffix: string, digits = 2): string {
  return value == null ? '暂无' : `${value.toFixed(digits)}${suffix}`
}

function render() {
  if (!container.value || !props.profile?.available || props.profile.history.length < 2) return
  chart ??= echarts.init(container.value, 'dark')
  const history = props.profile.history
  chart.setOption(
    {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis', valueFormatter: (v: number | string) => `${Number(v).toFixed(2)}%` },
      grid: { left: '8%', right: '4%', top: 20, bottom: 34 },
      xAxis: {
        type: 'category',
        data: history.map((p) => p.datetime.slice(0, 10)),
        boundaryGap: false,
        axisLabel: { formatter: (v: string) => v.slice(0, 7) },
      },
      yAxis: { type: 'value', name: '%', axisLabel: { formatter: (v: number) => `${v.toFixed(1)}%` } },
      dataZoom: [{ type: 'inside', start: 0, end: 100 }],
      series: [
        {
          name: '滚动股息率',
          type: 'line',
          data: history.map((p) => p.yield_pct),
          symbol: 'none',
          smooth: true,
          lineStyle: { width: 1.8, color: '#f0b90b' },
          areaStyle: { opacity: 0.12, color: '#f0b90b' },
        },
      ],
    },
    true,
  )
}

function resize() { chart?.resize() }
onMounted(() => {
  render()
  window.addEventListener('resize', resize)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chart?.dispose()
  chart = null
})
watch(() => props.profile, render, { deep: true })
</script>

<template>
  <div v-if="profile?.available" class="dividend-profile">
    <p v-if="profile.source" class="dividend-source">数据来源：{{ profile.source }}</p>
    <div class="dividend-summary">
      <div class="dividend-card highlight">
        <span>当前股息率</span>
        <strong>{{ formatValue(profile.current_yield_pct, '%') }}</strong>
      </div>
      <div class="dividend-card">
        <span>历史分位</span>
        <strong>{{ formatValue(profile.historical_percentile, '%', 1) }}</strong>
        <small v-if="profile.historical_percentile != null">当前值高于历史 {{ profile.historical_percentile.toFixed(1) }}% 的有效日</small>
        <small v-else>只有当前参考值，暂不能计算历史分位</small>
      </div>
      <div class="dividend-card">
        <span v-if="!referenceOnly">{{ indexDerived ? '近12个月现金分红（指数口径）' : '近12个月每股现金分红' }}</span>
        <span v-else>现金分红历史</span>
        <strong v-if="!referenceOnly">{{ formatValue(profile.current_dividend_per_share, indexDerived ? ' 指数点' : ' 元/股', 3) }}</strong>
        <strong v-else>暂无</strong>
        <small v-if="referenceOnly">仅有当前参考股息率，未提供逐次现金分红</small>
        <small v-else-if="profile.as_of">截至 {{ profile.as_of.slice(0, 10) }}</small>
        <small v-else>未提供逐次现金分红</small>
      </div>
    </div>
    <div v-if="profile.history.length >= 2" ref="container" class="dividend-chart"></div>
    <p class="dividend-note">{{ profile.note }}<template v-if="profile.event_count">；共 {{ profile.event_count }} 次现金分红事件。</template></p>
  </div>
  <div v-else class="dividend-empty">
    <span>暂无可验证的股息率</span>
    <small>{{ unavailableReason || profile?.note || '该标的当前没有可用的现金分红或行情源股息率数据。' }}</small>
  </div>
</template>

<style scoped>
.dividend-summary { display: flex; gap: 12px; flex-wrap: wrap; }
.dividend-card { flex: 1; min-width: 170px; padding: 12px 14px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-elevated); }
.dividend-card span, .dividend-card small { display: block; color: var(--text-dim); font-size: 12px; }
.dividend-card strong { display: block; margin: 5px 0; font: 600 21px var(--font-mono); color: var(--text); }
.dividend-card.highlight { border-color: rgba(240, 185, 11, .55); }
.dividend-card.highlight strong { color: #f0b90b; }
.dividend-chart { width: 100%; height: 250px; margin-top: 12px; }
.dividend-note, .dividend-empty { color: var(--text-dim); font-size: 12px; }
.dividend-source { margin: 0 0 8px; color: #f0b90b; font-size: 12px; }
.dividend-empty { padding: 16px; border: 1px dashed var(--border); border-radius: 8px; }
.dividend-empty span, .dividend-empty small { display: block; }
.dividend-empty small { margin-top: 5px; }
</style>
