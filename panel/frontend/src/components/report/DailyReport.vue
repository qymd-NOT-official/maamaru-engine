<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../api'
import type { DailyReport, DailyReportGoal } from '../../types'
import { dayLabel, eventTime, resourceLabel, resourceNames, runStatusLabel, scriptNames, shanghaiDate, signed } from './reportModel'

const emit = defineEmits<{ 'open-planning': [] }>()

// '' 表示今天；翻页只在上海时区的日历日之间走
const date = ref('')
const today = shanghaiDate(Date.now() / 1000)
const report = ref<DailyReport | null>(null)
const loading = ref(false)
const error = ref('')

const effectiveDate = computed(() => date.value || today)
const dateLabel = computed(() => dayLabel(effectiveDate.value))
const canGoNext = computed(() => effectiveDate.value >= today)

function shiftDate(current: string, deltaDays: number): string {
  const base = new Date(`${current}T00:00:00+08:00`).getTime()
  return shanghaiDate((base + deltaDays * 86400) / 1000)
}
function step(deltaDays: number) {
  if (deltaDays > 0 && canGoNext.value) return
  date.value = shiftDate(effectiveDate.value, deltaDays)
}
function backToToday() {
  date.value = ''
}

async function load() {
  loading.value = true
  try {
    report.value = await api.dailyReport(date.value)
    error.value = ''
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '日报读取失败'
  } finally {
    loading.value = false
  }
}
watch(date, load)
onMounted(load)

// 整个数据日什么都没有 → 一句话空态，不铺五个小节
const hasAnyData = computed(() => {
  const value = report.value
  if (!value) return false
  return Boolean(value.resources || value.drops || value.training
    || (value.goals && value.goals.length) || (value.attendance && value.attendance.length))
})

// ---- 今日收支 ----

const netRows = computed(() => {
  const net = report.value?.resources?.net || {}
  return resourceNames
    .map(name => ({ name, delta: net[name] }))
    .filter(row => row.delta != null && row.delta !== 0)
})

const balanceRows = computed(() => {
  const section = report.value?.resources
  if (!section?.opening || !section.closing) return []
  return resourceNames.flatMap(name => {
    const from = section.opening!.resources[name]
    const to = section.closing!.resources[name]
    if (from == null || to == null || from === to) return []
    return [{ name, from, to }]
  })
})

const clock = (capturedAt: string) => (capturedAt || '').slice(11, 16)

const entryRows = computed(() => report.value?.resources?.entries.slice(0, 8) || [])
const entryHint = computed(() => {
  const section = report.value?.resources
  if (!section) return ''
  const rest = section.entry_total - entryRows.value.length
  return rest > 0 ? `还有 ${rest} 笔小的没展开` : ''
})

// ---- 今日掉落 ----

const dropGroups = computed(() => report.value?.drops?.groups || [])

// ---- 今日练度 ----

const training = computed(() => report.value?.training)
const expRows = computed(() => (training.value?.exp_top || []).filter(row => row.exp_gain > 0))
const trainingEmptyText = computed(() => {
  const section = training.value
  if (!section) return ''
  if (!section.has_previous) return '狐之助还没攒够两次快照，练度从下次收账开始记。'
  return '今天没有练度变化，刀刀们原地踏步。'
})

// ---- 目标进度 ----

const goalRows = computed(() => report.value?.goals || [])
const goalStatusMeta: Record<string, { label: string; tone: string }> = {
  done: { label: '达成', tone: 'done' },
  on_track: { label: '赶得上', tone: 'track' },
  behind: { label: '要加劲', tone: 'behind' },
  active: { label: '进行中', tone: 'active' },
  expired: { label: '已过期', tone: 'dim' },
  unknown: { label: '待观察', tone: 'dim' },
}
function goalTitle(goal: DailyReportGoal) {
  const name = goal.fragment || goal.event || (goal.resource ? resourceLabel(goal.resource) : '小目标')
  return goal.target != null ? `${name} · 目标 ${goal.target.toLocaleString()}` : name
}

// ---- 今日出勤 ----

const attendanceRows = computed(() => report.value?.attendance || [])
function runName(row: { label: string | null; script: string | null }) {
  return (row.label && row.label.trim()) || scriptNames[row.script || ''] || row.script || '任务'
}
function runWindow(row: { started_at: number | null; ended_at: number | null }) {
  if (!row.started_at) return ''
  return row.ended_at ? `${eventTime(row.started_at)} ~ ${eventTime(row.ended_at)}` : `${eventTime(row.started_at)} 开始`
}
</script>

<template>
  <section class="daily-report" :class="{ loading }" aria-labelledby="daily-report-title">
    <header class="daily-report-header">
      <div>
        <h3 id="daily-report-title">日报</h3>
        <p v-if="report && !loading">狐之助代笔 · {{ dateLabel }}</p>
      </div>
      <div class="daily-report-nav">
        <button type="button" class="daily-report-step" aria-label="前一天" @click="step(-1)">‹</button>
        <span class="daily-report-date">{{ dateLabel }}</span>
        <button type="button" class="daily-report-step" :disabled="canGoNext" aria-label="后一天" @click="step(1)">›</button>
        <button v-if="date" type="button" class="daily-report-today" @click="backToToday">今天</button>
      </div>
    </header>

    <p v-if="error" class="daily-report-note" role="alert">{{ error }}</p>
    <p v-else-if="loading" class="daily-report-note">狐之助正在翻这一天的档案……</p>
    <p v-else-if="!hasAnyData" class="daily-report-note">这一天狐之助还没攒下记录，往前翻翻或者先跑一局。</p>

    <template v-else>
      <section v-if="report?.resources" class="daily-report-section" aria-label="今日收支">
        <h4>今日收支</h4>
        <ul v-if="netRows.length" class="daily-report-net">
          <li v-for="row in netRows" :key="row.name" :class="{ gain: row.delta! > 0, loss: row.delta! < 0 }">
            <span>{{ resourceLabel(row.name) }}</span><b>{{ signed(row.delta) }}</b>
          </li>
        </ul>
        <p v-if="balanceRows.length" class="daily-report-balance">
          首末读数
          <template v-for="row in balanceRows" :key="row.name"> · {{ resourceLabel(row.name) }} {{ row.from.toLocaleString() }} → {{ row.to.toLocaleString() }}</template>
        </p>
        <p v-else-if="report.resources.opening || report.resources.closing" class="daily-report-balance">
          读数时间 {{ report.resources.opening ? clock(report.resources.opening.captured_at) : '—' }} → {{ report.resources.closing ? clock(report.resources.closing.captured_at) : '—' }}
        </p>
        <ul v-if="entryRows.length" class="daily-report-list">
          <li v-for="(entry, index) in entryRows" :key="`${entry.ts}:${index}`">
            <span class="daily-report-entry-note">{{ entry.note || '来源未确认的一笔' }}</span>
            <b :class="{ gain: entry.delta > 0, loss: entry.delta < 0 }">{{ resourceLabel(entry.resource) }} {{ signed(entry.delta) }}</b>
            <time>{{ eventTime(entry.ts) }}</time>
          </li>
        </ul>
        <p v-if="entryHint" class="daily-report-more">{{ entryHint }}。</p>
      </section>

      <section v-if="dropGroups.length" class="daily-report-section" aria-label="今日掉落">
        <h4>今日掉落 <small>共 {{ report?.drops?.total }} 振<template v-if="report?.drops?.first_get_total">，初入手 {{ report.drops.first_get_total }} 振</template></small></h4>
        <div v-for="group in dropGroups" :key="group.label" class="daily-report-drop-group">
          <h5>{{ group.label }}<small>{{ group.count }} 振</small></h5>
          <ul class="daily-report-chips">
            <li v-for="(sword, index) in group.swords" :key="`${sword.name}:${index}`" :class="{ first: sword.is_first_get_sword }">
              {{ sword.name }}<em v-if="sword.is_first_get_sword">初入手!</em>
            </li>
          </ul>
        </div>
      </section>

      <section v-if="training" class="daily-report-section" aria-label="今日练度">
        <h4>今日练度 <small>{{ training.snapshot_captured_at.slice(5, 16) }} 收账 · {{ training.sword_count }} 振</small></h4>
        <template v-if="training.has_previous">
          <ul v-if="training.level_ups.length" class="daily-report-list">
            <li v-for="row in training.level_ups" :key="`lv-${row.serial_id}`">
              <span>{{ row.name }}</span><b>Lv.{{ row.from }} → {{ row.to }}</b>
            </li>
          </ul>
          <ul v-if="training.ranbu_ups.length" class="daily-report-list">
            <li v-for="row in training.ranbu_ups" :key="`rb-${row.serial_id}`">
              <span>{{ row.name }}</span><b>乱舞 {{ row.from }} → {{ row.to }}</b>
            </li>
          </ul>
          <ul v-if="expRows.length" class="daily-report-list">
            <li v-for="row in expRows" :key="`exp-${row.serial_id}`">
              <span>经验 {{ row.name }}</span><b>+{{ row.exp_gain.toLocaleString() }}<small v-if="row.exp != null">（共 {{ row.exp.toLocaleString() }}）</small></b>
            </li>
          </ul>
          <p v-if="!training.level_ups.length && !training.ranbu_ups.length && !expRows.length" class="daily-report-note">{{ trainingEmptyText }}</p>
        </template>
        <p v-else class="daily-report-note">{{ trainingEmptyText }}</p>
      </section>

      <section v-if="goalRows.length || report?.goals !== null" class="daily-report-section" aria-label="目标进度">
        <h4>目标进度</h4>
        <ul v-if="goalRows.length" class="daily-report-list">
          <li v-for="goal in goalRows" :key="`goal-${goal.id ?? goal.message}`">
            <span><i class="daily-report-goal-status" :data-tone="goalStatusMeta[goal.status]?.tone || 'dim'">{{ goalStatusMeta[goal.status]?.label || goal.status }}</i>{{ goalTitle(goal) }}</span>
            <p class="daily-report-goal-message">{{ goal.message }}</p>
          </li>
        </ul>
        <p v-else class="daily-report-note">还没有立小目标。<button type="button" class="daily-report-link" @click="emit('open-planning')">去「规划」立一个</button>，狐之助帮你盯着。</p>
      </section>

      <section v-if="attendanceRows.length" class="daily-report-section" aria-label="今日出勤">
        <h4>今日出勤 <small>まあ丸出动 {{ attendanceRows.length }} 次</small></h4>
        <ul class="daily-report-list">
          <li v-for="row in attendanceRows" :key="row.run_id || `${row.started_at}`">
            <span>{{ runName(row) }}</span>
            <b :data-status="row.status">{{ runStatusLabel(row) }}</b>
            <time>{{ runWindow(row) }}</time>
          </li>
        </ul>
      </section>
    </template>
  </section>
</template>

<style scoped>
.daily-report { padding: 18px 20px; background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 12px; }
.daily-report-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.daily-report-header h3 { margin: 0; }
.daily-report-header p { margin: 3px 0 0; color: var(--ink-dim); font-size: 12px; }
.daily-report-nav { display: flex; align-items: center; gap: 6px; }
.daily-report-step { min-width: 30px; min-height: 30px; padding: 0 8px; color: var(--ink); background: var(--paper); border: 1px solid var(--paper-line); border-radius: 8px; font-size: 16px; cursor: pointer; }
.daily-report-step:hover:not(:disabled) { border-color: var(--fox-gold); }
.daily-report-step:disabled { color: var(--ink-dim); opacity: .45; cursor: default; }
.daily-report-date { min-width: 64px; color: var(--ink); font-variant-numeric: tabular-nums; text-align: center; }
.daily-report-today { min-height: 30px; padding: 0 10px; color: var(--fox-gold-deep); background: var(--fox-gold-pale); border: 1px solid var(--fox-gold); border-radius: 8px; font-size: 12px; cursor: pointer; }
.daily-report-note { margin: 10px 0 0; color: var(--ink-dim); font-size: 12px; line-height: 1.6; }
.daily-report-link { padding: 0 2px; color: var(--fox-gold-deep); background: transparent; border: 0; border-bottom: 1px dashed var(--fox-gold); font-size: inherit; cursor: pointer; }
.daily-report-section { margin-top: 14px; padding-top: 12px; border-top: 1px dashed var(--paper-line); }
.daily-report-section h4 { display: flex; align-items: baseline; gap: 8px; margin: 0 0 8px; font-size: 14px; }
.daily-report-section h4 small, .daily-report-drop-group h5 small { color: var(--ink-dim); font-size: 11px; font-weight: 400; }
.daily-report-net { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 8px; padding: 0; list-style: none; }
.daily-report-net li { display: flex; align-items: baseline; gap: 6px; padding: 4px 9px; background: var(--paper); border: 1px solid var(--paper-line); border-radius: 999px; font-size: 12px; }
.daily-report-net b, .daily-report-list b { font-variant-numeric: tabular-nums; }
.daily-report-net .gain b, .daily-report-list .gain { color: #47734f; }
.daily-report-net .loss b, .daily-report-list .loss { color: var(--danger); }
.daily-report-balance { margin: 0 0 8px; color: var(--ink-dim); font-size: 11px; line-height: 1.6; }
.daily-report-list { display: grid; gap: 6px; margin: 0; padding: 0; list-style: none; }
.daily-report-list li { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: baseline; gap: 4px 10px; padding: 6px 8px; background: var(--paper); border-radius: 8px; font-size: 12px; }
.daily-report-list li > span { min-width: 0; overflow-wrap: anywhere; }
.daily-report-list li > time { grid-column: 1 / -1; color: var(--ink-dim); font-size: 10px; }
.daily-report-list li > b small { color: var(--ink-dim); font-weight: 400; }
.daily-report-more { margin: 6px 0 0; color: var(--ink-dim); font-size: 11px; }
.daily-report-drop-group { margin-top: 8px; }
.daily-report-drop-group h5 { display: flex; align-items: baseline; gap: 8px; margin: 0 0 6px; font-size: 12px; }
.daily-report-chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0; padding: 0; list-style: none; }
.daily-report-chips li { padding: 3px 9px; background: var(--paper); border: 1px solid var(--paper-line); border-radius: 999px; font-size: 12px; }
.daily-report-chips li.first { color: #7a4b16; background: var(--fox-gold-pale); border-color: var(--fox-gold); font-weight: 700; }
.daily-report-chips li em { margin-left: 5px; color: var(--fox-gold-deep); font-size: 10px; font-style: normal; }
.daily-report-goal-status { margin-right: 7px; padding: 1px 6px; border-radius: 999px; font-size: 10px; font-style: normal; font-weight: 700; }
.daily-report-goal-status[data-tone='done'], .daily-report-goal-status[data-tone='track'] { color: #426b36; background: #edf5e8; }
.daily-report-goal-status[data-tone='behind'] { color: var(--danger); background: #f7e6e1; }
.daily-report-goal-status[data-tone='active'] { color: #536f8a; background: #edf2f6; }
.daily-report-goal-status[data-tone='dim'] { color: var(--ink-dim); background: var(--paper-panel); }
.daily-report-goal-message { grid-column: 1 / -1; margin: 0; color: var(--ink-dim); font-size: 11px; line-height: 1.55; }
.daily-report-list b[data-status='completed'] { color: #426b36; }
.daily-report-list b[data-status='failed'] { color: var(--danger); }
.daily-report-list b[data-status='stopped'] { color: #8d6a3e; }
@media (max-width: 520px) {
  .daily-report { padding: 14px; }
  .daily-report-header { flex-direction: column; }
  .daily-report-list li { grid-template-columns: minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) {
  .daily-report * { transition: none !important; animation: none !important; }
}
</style>
