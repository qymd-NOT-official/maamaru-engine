<script setup lang="ts">
import { computed } from 'vue'
import type { HumanReport, InventoryGap, LedgerAttribution, ManualSession } from '../../types'
import { resourceLabel, categoryLabel, dayLabel, eventTime, recordOrigin, runTitle, signed } from './reportModel'

const props = defineProps<{
  readOnly?: boolean
  date: string
  resource: string
  totalDelta: number | null
  claimedAmount: number
  unexplained: number | null
  highlightCategory?: string
  attributions: LedgerAttribution[]
  runs: any[]
  manualReports: HumanReport[]
  manualSessions: ManualSession[]
  gaps: InventoryGap[]
}>()

const emit = defineEmits<{ close: []; report: [gap: InventoryGap]; 'report-day': []; 'open-records': [date: string] }>()

const sortedAttributions = computed(() => [...props.attributions].sort((a, b) => a.ts - b.ts))
// 连续相同的归因（同标签同金额，如连续补充提灯）折叠成一条，显示次数与合计
const groupedAttributions = computed(() => {
  const groups: { ts: number; tsEnd: number; label: string; source: string; script: string; delta: number; count: number }[] = []
  for (const item of sortedAttributions.value) {
    const label = item.label || categoryLabel(item.source)
    const script = recordOrigin(item, props.runs)
    const delta = Number(item.delta || 0)
    const last = groups[groups.length - 1]
    if (last && last.label === label && last.script === script && last.delta === delta) {
      last.count += 1
      last.tsEnd = item.ts
    } else {
      groups.push({ ts: item.ts, tsEnd: item.ts, label, source: item.source, script, delta, count: 1 })
    }
  }
  return groups
})
const attributedTotal = computed(() => props.attributions.reduce((sum, item) => sum + Number(item.delta || 0), 0))
const needsRecall = computed(() => Boolean(props.unexplained || props.gaps.length))

interface RecallClue { key: string; owner: '你' | 'まあ丸'; title: string; detail: string }

function reportSource(report: HumanReport): string {
  return report.activities?.find(value => !['暂不说明', '记不清了', '没有其他操作'].includes(value)) || '没写来源'
}

function runSummary(): string {
  const groups = new Map<string, { count: number; loops: number }>()
  for (const run of props.runs) {
    const script = runTitle({ ...run, loops: 0 })
    const current = groups.get(script) || { count: 0, loops: 0 }
    current.count += 1
    current.loops += Number(run.loops || 0)
    groups.set(script, current)
  }
  return [...groups.entries()].map(([script, value]) => {
    const name = script
    return value.loops > 0 ? `${name} ${value.loops} 圈` : `${name} ${value.count} 次`
  }).join(' · ')
}

const recallClues = computed<RecallClue[]>(() => {
  const clues: RecallClue[] = [...props.manualReports]
    .sort((a, b) => Number(a.occurred_at) - Number(b.occurred_at))
    .map(report => ({
      key: `report:${report.id}`, owner: '你',
      title: `你在这天记过「${reportSource(report)}」`,
      detail: `${eventTime(Number(report.occurred_at))} · ${report.resource} ${signed(Number(report.claimed_delta))}${report.note ? ` · ${report.note}` : ''}`,
    }))
  for (const item of [...props.manualSessions].sort((a, b) => a.started_at - b.started_at)) {
    clues.push({
      key: `session:${item.id}`, owner: '你', title: `你补记过「${item.activity}」`,
      detail: `${eventTime(item.started_at)} → ${eventTime(item.ended_at)} · ${item.loops} 圈${item.note ? ` · ${item.note}` : ''}`,
    })
  }
  if (props.runs.length) clues.push({
    key: 'runs', owner: 'まあ丸', title: 'まあ丸当天跑过这些任务', detail: runSummary(),
  })
  return clues.slice(0, 4)
})

function gapDelta(gap: InventoryGap): string {
  const order = ['小判', '木炭', '玉钢', '冷却材', '砥石', '委托符', '加速符']
  return order.filter(name => gap.resource_delta?.[name])
    .map(name => `${name} ${signed(Number(gap.resource_delta![name]))}`).join(' · ')
}

function recordDateLabel(date: string): string {
  const [, month, day] = date.split('-').map(Number)
  return `${month}月${day}日`
}
</script>

<template>
  <section class="day-detail" aria-live="polite">
    <header>
      <div>
        <h4>{{ dayLabel(date) }} · {{ resourceLabel(resource) }} {{ signed(totalDelta) }}<template v-if="highlightCategory"> · {{ categoryLabel(highlightCategory) }}</template></h4>
      </div>
      <button type="button" class="secondary" @click="emit('close')">收起</button>
    </header>

    <p class="day-detail-total">
      已记录 {{ signed(attributedTotal) }}<template v-if="claimedAmount"> · 你补记了 {{ signed(claimedAmount) }}</template><template v-if="unexplained"> · 还有 <b>{{ signed(unexplained) }}</b> 来源未确认</template>
    </p>

    <details v-if="needsRecall && recallClues.length" class="day-detail-recall" aria-label="回忆线索">
      <summary>查看当天线索</summary>
      <ul v-if="recallClues.length">
        <li v-for="clue in recallClues" :key="clue.key">
          <b>{{ clue.owner }}</b><span><strong>{{ clue.title }}</strong><small>{{ clue.detail }}</small></span>
        </li>
      </ul>
    </details>

    <ul v-if="groupedAttributions.length" class="day-detail-attributions">
      <li v-for="group in groupedAttributions" :key="`${group.ts}:${group.label}`">
        <time>{{ eventTime(group.ts) }}<template v-if="group.count > 1"> → {{ eventTime(group.tsEnd) }}</template></time>
        <span><b>{{ group.label }}<template v-if="group.count > 1"> ×{{ group.count }}</template></b><small>{{ group.script }} · {{ resourceLabel(resource) }} {{ signed(group.delta) }}<template v-if="group.count > 1"> · 共 {{ signed(group.delta * group.count) }}</template></small></span>
      </li>
    </ul>
    <p v-else class="day-detail-empty">这天没有自动对上来源的{{ resourceLabel(resource) }}流水。</p>

    <button v-if="runs.length" type="button" class="day-detail-records" @click="emit('open-records', date)">
      <span>当天共执行 <b>{{ runs.length }}</b> 次任务</span>
      <em>查看 {{ recordDateLabel(date) }}全部记录 →</em>
    </button>

    <div v-for="gap in gaps" :key="gap.gap_key" class="day-detail-gap">
      <div>
        <strong>🦊 这段家底变化还没对上账</strong>
        <p>{{ gapDelta(gap) }}</p>
        <small>{{ eventTime(gap.started_at) }} → {{ eventTime(gap.ended_at) }} · 看完上面的线索再补</small>
      </div>
      <button v-if="!readOnly" type="button" class="primary" @click="emit('report', gap)">补账</button>
    </div>

    <div v-if="!readOnly && !gaps.length && unexplained" class="day-detail-gap">
      <button type="button" class="primary" @click="emit('report-day')">补账</button>
    </div>
  </section>
</template>

<style scoped>
.day-detail-recall summary { cursor: pointer; color: var(--ink-dim); font-size: 12px; }
.day-detail { background: var(--paper); border: 1px solid var(--paper-line); border-radius: 12px; padding: 14px 16px; display: flex; flex-direction: column; gap: 12px; }
.day-detail > header { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.day-detail h4 { margin: 2px 0 0; }
.day-detail small { color: var(--ink-dim); }
.day-detail-total { margin: 0; color: var(--ink-dim); }
.day-detail-total b { color: var(--ink); }
.day-detail-recall { padding: 0; }
.day-detail-recall > header { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 9px; }
.day-detail-recall ul { display: flex; flex-direction: column; gap: 7px; list-style: none; padding: 0; margin: 0; }
.day-detail-recall li { display: grid; grid-template-columns: 42px minmax(0, 1fr); align-items: start; gap: 9px; }
.day-detail-recall li > b { padding: 3px 6px; color: var(--fox-gold-deep); background: var(--paper); border: 1px solid var(--paper-line); border-radius: 999px; font-size: 11px; text-align: center; }
.day-detail-recall li span { display: flex; flex-direction: column; min-width: 0; }
.day-detail-recall li small { overflow-wrap: anywhere; }
.day-detail-recall p { margin: 0; color: var(--ink-dim); }
.day-detail-attributions { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.day-detail-attributions li { display: flex; gap: 10px; align-items: baseline; }
.day-detail-attributions time { color: var(--ink-dim); font-size: 12px; white-space: nowrap; }
.day-detail-attributions span { display: flex; flex-direction: column; }
.day-detail-records { display: flex; justify-content: space-between; align-items: center; gap: 12px; width: 100%; padding: 11px 12px; color: var(--ink); background: transparent; border: 0; border-top: 1px dashed var(--paper-line); text-align: left; }
.day-detail-records:hover { color: var(--fox-gold-deep); background: var(--fox-gold-pale); }
.day-detail-records em { color: var(--fox-gold-deep); font-style: normal; white-space: nowrap; }
.day-detail-empty { margin: 0; color: var(--ink-dim); }
.day-detail-gap { display: flex; justify-content: flex-end; align-items: center; gap: 12px; flex-wrap: wrap; padding: 6px 0; }
.day-detail-gap p { margin: 2px 0; }
@media (max-width: 600px) {
  .day-detail-recall > header { align-items: flex-start; flex-direction: column; gap: 2px; }
  .day-detail-gap .primary { width: 100%; }
}
</style>
