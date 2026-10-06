<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { dayLabel, resourceColors, resourceLabel } from './reportModel'
import type { BalancePoint } from '../../types'

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const props = withDefaults(defineProps<{
  points: BalancePoint[]
  resources?: string[]
  selectedDate?: string
  loading?: boolean
}>(), { resources: () => [], selectedDate: '', loading: false })

const emit = defineEmits<{ select: [payload: { date: string; key: string }] }>()

const box = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

function cssVar(name: string, fallback: string) {
  return getComputedStyle(box.value || document.documentElement).getPropertyValue(name).trim() || fallback
}

function axisLabels() {
  const points = props.points
  const dayCounts = new Map<string, number>()
  for (const point of points) dayCounts.set(point.date, (dayCounts.get(point.date) || 0) + 1)
  const span = points.length > 1 ? points[points.length - 1].ts - points[0].ts : 0
  // 一天内多个读数、或整体跨度不到两天时，标签带时分，避免重复的日期挤在一起
  const withTime = dayCounts.size !== points.length || span < 2 * 86400
  return points.map(point => {
    const label = dayLabel(point.date)
    if (!withTime) return label
    const time = new Date(point.ts * 1000).toLocaleString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai' })
    return `${label} ${time}`
  })
}

function buildOption() {
  const ink = cssVar('--ink', '#3d3229')
  const inkDim = cssVar('--ink-dim', '#8a7f72')
  const line = cssVar('--paper-line', '#ddd6cb')
  const card = cssVar('--paper-card', '#faf6ef')
  const selected = props.selectedDate
  return {
    grid: { left: 12, right: 16, top: 30, bottom: 8, containLabel: true },
    legend: { top: 0, itemWidth: 14, itemHeight: 8, textStyle: { color: inkDim, fontSize: 12 } },
    tooltip: {
      trigger: 'axis',
      backgroundColor: card,
      borderColor: line,
      textStyle: { color: ink, fontSize: 13 },
      formatter(params: any) {
        const items = (Array.isArray(params) ? params : [params]).filter((item: any) => typeof item.value === 'number' && Number.isFinite(item.value))
        if (!items.length) return ''
        const rows = items.map((item: any) =>
          `<div style="display:flex;justify-content:space-between;gap:16px"><span>${item.marker}${item.seriesName}</span><b>${Number(item.value).toLocaleString()}</b></div>`,
        ).join('')
        return `<div style="min-width:180px"><div style="margin-bottom:4px"><b>${items[0].axisValue}</b></div>${rows}</div>`
      },
    },
    xAxis: {
      type: 'category',
      data: axisLabels(),
      axisLine: { lineStyle: { color: line } },
      axisTick: { show: false },
      axisLabel: { color: inkDim, fontSize: 12 },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: line, type: 'dashed' } },
      axisLabel: { color: inkDim, fontSize: 12, formatter: (value: number) => value >= 10000 ? `${value / 10000}万` : String(value) },
    },
    series: props.resources.map(name => ({
      id: name,
      name: resourceLabel(name),
      type: 'line',
      color: resourceColors[name] || '#8a7f72',
      connectNulls: true,
      symbolSize: 7,
      lineStyle: { width: 2 },
      emphasis: { focus: 'series' },
      data: props.points.map(point => ({
        value: point.values?.[name] ?? null,
        itemStyle: selected && point.date === selected
          ? { borderColor: ink, borderWidth: 2 }
          : undefined,
      })),
    })),
  }
}

function render() {
  if (!chart && box.value) {
    chart = echarts.init(box.value)
    chart.on('click', (params: any) => {
      const point = props.points[params.dataIndex]
      if (point) emit('select', { date: point.date, key: String(params.seriesId || '') })
    })
  }
  if (chart) chart.setOption(buildOption(), true)
}

onMounted(() => {
  render()
  if (box.value) {
    observer = new ResizeObserver(() => { chart?.resize(); render() })
    observer.observe(box.value)
  }
})

watch(() => [props.points, props.resources, props.selectedDate], render, { deep: true })

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="resource-echart" :class="{ loading }">
    <div ref="box" class="resource-echart-box" role="img" aria-label="资源余额折线图"></div>
    <p v-if="loading" class="resource-echart-hint">狐之助正在整理这段时间的余额……</p>
    <p v-else-if="points.length < 2" class="resource-echart-hint">同一时间段至少需要两次库存读数，狐之助再攒一会儿账。</p>
  </div>
</template>

<style scoped>
.resource-echart { position: relative; }
.resource-echart-box { width: 100%; height: 320px; }
.resource-echart-hint { position: absolute; inset: 0; display: grid; place-items: center; color: var(--ink-dim); background: color-mix(in srgb, var(--paper-card) 70%, transparent); margin: 0; }
</style>
