<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ActivityPace, DayTimeline, EventAbacus, EventTimelineCandidate, EventTimelineEntry, EventTimelineReport, PlanningGoalAdvice } from '../../types'
import { dailyRunTarget, gameDaysRemaining, immediateBatchPlan } from './eventTimelineModel'

const props = defineProps<{
  timeline: EventTimelineReport | null
  abacuses: EventAbacus[]
  goals: PlanningGoalAdvice[]
  loading?: boolean
  error?: string
  estimateSaving?: string
  goalSaving?: string
  targetSaving?: string
  targetError?: string
  activityPaces?: Record<string, ActivityPace[]>
  dayTimeline?: DayTimeline | null
}>()

const emit = defineEmits<{
  (event: 'save-estimate', name: string, value: number): void
  (event: 'add-goal', abacus: EventAbacus): void
  (event: 'add-stock-goal', abacus: EventAbacus, target: number): void
  (event: 'save-tama-target', name: string, target: number): void
  (event: 'open-activity', script: 'hanafuda' | 'raid', loops: number): void
  (event: 'open-raid-recommendation'): void
}>()

const estimateInputs = ref<Record<string, string>>({})
const targetInputs = ref<Record<string, string>>({})
const tamaTargetInputs = ref<Record<string, string>>({})
const paceWindows = ref<Record<string, 'event' | '1h' | '3h'>>({})
const paceSources = ref<Record<string, 'maamaru' | 'manual'>>({})
const targetDialog = ref<HTMLDialogElement>()
const targetEntry = ref<EventTimelineEntry>()
function editTarget(entry: EventTimelineEntry) { targetEntry.value = entry; targetDialog.value?.showModal() }
watch(() => props.targetSaving, (value, previous) => {
  const entry = targetEntry.value
  if (previous && !value && !props.targetError && entry && abacusByName.value.get(entry.name)?.tama_target === Number(tamaTargetInputs.value[entry.name])) targetDialog.value?.close()
})
const nowMs = ref(Date.now())
let clockTimer: number | undefined

onMounted(() => {
  clockTimer = window.setInterval(() => { nowMs.value = Date.now() }, 60_000)
})
onBeforeUnmount(() => window.clearInterval(clockTimer))

watch(() => props.abacuses, (items) => {
  for (const item of items) {
    if (item.keys_per_run != null) estimateInputs.value[item.event] = String(item.keys_per_run)
    if (isCurrencyMechanics(item.mechanics) && item.tama_target != null) tamaTargetInputs.value[item.event] = String(item.tama_target)
  }
}, { immediate: true })

watch(() => props.goals, (items) => {
  for (const item of items) {
    if (item.event && item.goal_mode === 'stock_target') targetInputs.value[item.event] = String(item.target)
  }
}, { immediate: true })

const abacusByName = computed(() => new Map(props.abacuses.map(item => [item.event, item])))
const goalByEvent = computed(() => new Map(props.goals.filter(item => item.event).map(item => [item.event!, item])))
const activeGroups = computed(() => [
  { key: 'ongoing', title: '正在进行', items: props.timeline?.ongoing || [] },
  { key: 'upcoming', title: '即将开始', items: props.timeline?.upcoming || [] },
  { key: 'ended', title: '刚收官', items: props.timeline?.ended || [] },
].filter(group => group.items.length))
const hasFormalEvents = computed(() => activeGroups.value.length > 0 || Boolean(props.timeline?.later.length))

function fmt(value: number | null | undefined) {
  return value == null ? '—' : Math.round(value).toLocaleString()
}

function parseDate(value: string) {
  return new Date(value.includes('T') ? value : `${value}T00:00:00+08:00`)
}

function shortDate(value: string | null | undefined) {
  if (!value) return '日期待定'
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', timeZone: 'Asia/Shanghai' }).format(parseDate(value))
}

function dateTime(value: string | null, fallback: string | null) {
  if (!value) return shortDate(fallback)
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai',
  }).format(parseDate(value))
}

function clockTime(value: string | null) {
  if (!value) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai',
  }).format(parseDate(value))
}

function eventRange(entry: EventTimelineEntry) {
  return `${dateTime(entry.start_at, entry.start_date)} → ${dateTime(entry.end_at, entry.end_date)}`
}

function axisMoment(entry: EventTimelineEntry, group: string) {
  if (group === 'ended') return `${shortDate(entry.end_at || entry.end_date)} 收摊`
  if (group === 'ongoing') {
    if (entry.days_left === 0) return '今天结束'
    if (entry.days_left === 1) return '明天结束'
    return entry.days_left == null ? '进行中' : `还剩 ${entry.days_left} 天`
  }
  if (entry.days_until_start === 0) return entry.start_at ? `今天 ${clockTime(entry.start_at)}` : '今天开始'
  if (entry.days_until_start === 1) return entry.start_at ? `明天 ${clockTime(entry.start_at)}` : '明天开始'
  return `${shortDate(entry.start_at || entry.start_date)} 开始`
}

function abacusFor(entry: EventTimelineEntry) {
  return abacusByName.value.get(entry.name)
}

// 代币档线型活动（花札「玉」、联队战「夜光贝」）：同一套规划卡，文案按 currency 走
const CURRENCY_MECHANICS = new Set(['hanafuda', 'raid'])

function isCurrencyMechanics(mechanics: string | null | undefined) {
  return CURRENCY_MECHANICS.has(String(mechanics || ''))
}

function currencyOf(entry: EventTimelineEntry) {
  return entry.budget?.currency || abacusFor(entry)?.currency || '玉'
}

function currencyScript(mechanics: string | null | undefined): 'hanafuda' | 'raid' {
  return mechanics === 'raid' ? 'raid' : 'hanafuda'
}


function goalFor(entry: EventTimelineEntry) {
  return goalByEvent.value.get(entry.name)
}

function paceOptions(entry: EventTimelineEntry) {
  return props.activityPaces?.[entry.name] || []
}

function paceFor(entry: EventTimelineEntry) {
  const options = paceOptions(entry)
  const selected = paceSources.value[entry.name]
  return options.find(item => item.source === selected)
    || options.find(item => item.source === 'maamaru') || options[0]
}

function setPaceSource(entry: EventTimelineEntry, source: 'maamaru' | 'manual') {
  paceSources.value[entry.name] = source
}

function paceSourceLabel(source: 'maamaru' | 'manual') {
  return source === 'maamaru' ? 'まあ丸实测' : '你手动记的'
}

function selectedPaceWindow(entry: EventTimelineEntry) {
  return paceWindows.value[entry.name] || 'event'
}

function setPaceWindow(entry: EventTimelineEntry, value: 'event' | '1h' | '3h') {
  paceWindows.value[entry.name] = value
}

function availablePaceSeconds(entry: EventTimelineEntry) {
  const selected = selectedPaceWindow(entry)
  if (selected === '1h') return 3600
  if (selected === '3h') return 10800
  if (!entry.end_at) return 0
  return Math.max(0, Math.floor((parseDate(entry.end_at).getTime() - nowMs.value) / 1000))
}

function possibleLoops(entry: EventTimelineEntry) {
  const pace = paceFor(entry)
  if (!pace) return null
  return Math.max(0, Math.floor(availablePaceSeconds(entry) / pace.secondsPerLoop))
}

function paceDuration(seconds: number) {
  const value = Math.max(0, Math.round(seconds))
  const hours = Math.floor(value / 3600)
  const minutes = Math.floor((value % 3600) / 60)
  if (!hours) return `${minutes} 分钟`
  return `${hours} 小时${minutes ? ` ${minutes} 分` : ''}`
}

function loopPace(seconds: number) {
  const value = Math.max(0, Math.round(seconds))
  return `${Math.floor(value / 60)}分${String(value % 60).padStart(2, '0')}秒/圈`
}

function paceSampleDate(timestamp: number) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', timeZone: 'Asia/Shanghai',
  }).format(new Date(timestamp * 1000))
}

function observedTime(timestamp: number) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    timeZone: 'Asia/Shanghai',
  }).format(new Date(timestamp * 1000))
}

function supportsTokenLearning(entry: EventTimelineEntry) {
  return Boolean(abacusFor(entry)?.keys_total)
}

function experienceLabel(entry: EventTimelineEntry) {
  const source = abacusFor(entry)?.keys_source
  if (source === 'measured') return '本期实测'
  if (source === 'history') return '参考上期'
  if (source === 'estimate') return '临时估计'
  return '第一次参加'
}

function eventSummary(entry: EventTimelineEntry) {
  const abacus = abacusFor(entry)
  if (!abacus) return entry.note
  if (isCurrencyMechanics(abacus.mechanics)) {
    const currency = abacus.currency || '玉'
    const goalLabel = abacus.tama_target_custom ? '本期目标' : '最高档'
    if (abacus.tama_current == null) return `目标${goalLabel} ${fmt(abacus.tama_target || 300000)} ${currency}`
    if (!abacus.tama_remaining) return `${goalLabel}已经拿到啦 🎉`
    return `已攒 ${fmt(abacus.tama_current)} ${currency}，离${goalLabel}还差 ${fmt(abacus.tama_remaining)} ${currency}`
  }
  if (abacus.runs_needed != null) {
    const ticketNote = entry.budget?.koban_cost === 0 ? ' · 免费手形够目标' : ''
    if (abacus.keys_obtained) {
      if (abacus.runs_needed === 0) return '四座宝库已全开 🎉'
      return `已拿 ${fmt(abacus.keys_obtained)} 把 · 再打约 ${fmt(abacus.runs_needed)} 圈全开${ticketNote}`
    }
    return `全开预计 ${fmt(abacus.runs_needed)} 圈${ticketNote}`
  }
  return abacus.message || entry.note
}

function submitEstimate(entry: EventTimelineEntry) {
  const value = Number(estimateInputs.value[entry.name])
  emit('save-estimate', entry.name, value)
}

function submitStockGoal(entry: EventTimelineEntry) {
  const abacus = abacusFor(entry)
  if (abacus) emit('add-stock-goal', abacus, Number(targetInputs.value[entry.name]))
}

function submitTamaTarget(entry: EventTimelineEntry) {
  emit('save-tama-target', entry.name, Number(tamaTargetInputs.value[entry.name]))
}

function tamaProgress(entry: EventTimelineEntry) {
  const target = entry.budget?.tama_target
  if (!target) return 0
  return Math.min(100, Math.max(0, Number(entry.budget?.tama_current || 0) / target * 100))
}

function tamaEstimateText(entry: EventTimelineEntry) {
  const budget = entry.budget
  if (!budget) return ''
  const currency = currencyOf(entry)
  if (budget.runs_needed == null) {
    return budget.tama_samples
      ? `已记 ${fmt(budget.tama_samples)} 圈，样本还不够稳，再完成几圈后可估算。`
      : `平均每圈带多少${currency}还没数过，再完成几圈后可估算。`
  }
  if (budget.runs_needed === 0) return ''
  const time = budget.estimated_seconds != null ? `，挂机约 ${paceDuration(budget.estimated_seconds)}` : ''
  return `按本期 ${fmt(budget.tama_samples)} 圈实测平均每圈约 ${fmt(budget.tama_per_loop)} ${currency}，还要打约 ${fmt(budget.runs_needed)} 圈${time}。`
}

function tamaRemainingText(entry: EventTimelineEntry) {
  const budget = entry.budget
  if (budget?.runs_needed == null) return tamaEstimateText(entry)
  if (!budget.runs_needed) return '目标已达成'
  return `还需约 ${fmt(budget.runs_needed)} 圈${budget.estimated_seconds != null ? `，约 ${paceDuration(budget.estimated_seconds)}` : ''}`
}

function tamaTimeText(entry: EventTimelineEntry) {
  const budget = entry.budget
  if (!budget || budget.can_finish == null || budget.estimated_seconds == null) return ''
  const secondsToEnd = budget.seconds_to_end ?? 0
  if (!budget.can_finish) return `离收摊只剩 ${paceDuration(secondsToEnd)}，照现在的圈速，即使全天挂机也来不及。`
  const daysLeft = gameDaysRemaining(nowMs.value, nowMs.value + secondsToEnd * 1000)
  const dailySeconds = budget.estimated_seconds / daysLeft
  const dailyRuns = dailyRunTarget(budget.runs_needed, secondsToEnd, nowMs.value)
  if (budget.mechanics === 'raid' && dailyRuns != null) {
    return `按剩余活动时间折算，日均约 ${fmt(dailyRuns)} 圈；今日安排见上方时间表。`
  }
  const action = dailyRuns == null
    ? `平均每天要留约 ${paceDuration(dailySeconds)}`
    : `平均每天挂约 ${fmt(dailyRuns)} 圈（约 ${paceDuration(dailySeconds)}）就来得及`
  return `离收摊还有 ${paceDuration(secondsToEnd)}，按现在的效率，${action}。`
}

function tamaBatchPlan(entry: EventTimelineEntry) {
  const budget = entry.budget
  if (!budget) return null
  const pace = paceFor(entry)?.secondsPerLoop ?? budget.seconds_per_loop
  return immediateBatchPlan(
    budget.runs_needed,
    pace,
    budget.seconds_to_end,
    nowMs.value,
  )
}

function shanghaiClock(timestamp: number) {
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai',
  }).format(new Date(timestamp))
}

function tamaBatchNote(entry: EventTimelineEntry) {
  const plan = tamaBatchPlan(entry)
  if (!plan) return ''
  const horizon = plan.horizon === 'daily-reset' ? '4:00 日课刷新' : '活动收摊'
  if (!plan.runs) return `${horizon}快到了，这一锅先别开；回本丸收尾后再继续。`
  const reserve = Math.round(plan.reserveSeconds / 60)
  return `预计 ${shanghaiClock(plan.finishAtMs)} 收工 · 给 ${horizon}留约 ${reserve} 分钟`
}

function openTamaBatch(entry: EventTimelineEntry) {
  const plan = tamaBatchPlan(entry)
  if (plan?.runs) emit('open-activity', currencyScript(entry.budget?.mechanics), plan.runs)
}

function tamaTicketText(entry: EventTimelineEntry) {
  const budget = entry.budget
  if (!budget || budget.runs_needed === 0 || budget.free_tickets_remaining == null) return ''
  const currency = currencyOf(entry)
  const base = `到收摊白票还能领 ${fmt(budget.free_tickets_remaining)} 张`
  if (budget.paid_tickets == null) return `${base}（手上现存的没算进来）。`
  if (budget.paid_tickets > 0) {
    if (budget.koban_cost != null) {
      return `${base}；手头现有令牌还没读，先按 0 张保守算，最多再补 ${fmt(budget.paid_tickets)} 张 ≈ ${fmt(budget.koban_cost)} 小判。`
    }
    return `${base}；手头现有令牌还没读，先按 0 张保守算，最多再补 ${fmt(budget.paid_tickets)} 张（票价还没核实，先不折算）。`
  }
  return `${base}，只算这些白票也已够，不用花钱补票。`
}

function budgetText(entry: EventTimelineEntry) {
  const budget = entry.budget
  if (!budget) return ''
  if (budget.koban_cost === 0) return ''
  const tickets = budget.paid_tickets != null ? `补票约 ${fmt(budget.paid_tickets)} 张，折 ` : ''
  if (budget.sufficient === true) return `${tickets}${fmt(budget.koban_cost)} 小判，家底已经备齐。`
  if (budget.shortfall != null) return `${tickets}${fmt(budget.koban_cost)} 小判，目前还差 ${fmt(budget.shortfall)}。`
  return budget.message
}

function budgetHeadline(entry: EventTimelineEntry) {
  const budget = entry.budget
  if (!budget) return ''
  if (budget.koban_cost === 0) return '无需额外小判'
  if (budget.sufficient === true) return '已经备齐'
  return `${fmt(budget.shortfall)} 小判`
}

function candidateName(candidate: EventTimelineCandidate) {
  return candidate.name || candidate.section || '未识别活动'
}

function candidateRange(candidate: EventTimelineCandidate) {
  if (!candidate.start_at && !candidate.end_at) return '时间待核对'
  return `${dateTime(candidate.start_at, null)} → ${dateTime(candidate.end_at, null)}`
}
</script>

<template>
  <section class="event-timeline-card">
    <header class="timeline-heading">
      <div>
        <h4>活动</h4>
      </div>
      <span v-if="timeline?.calendar_stale" class="timeline-stale">日历可能不是最新</span>
    </header>

    <p v-if="error" class="timeline-error">{{ error }}</p>
    <div v-else-if="loading && !timeline" class="timeline-empty">正在整理近期活动……</div>
    <div v-else-if="timeline">
      <div v-if="activeGroups.length" class="timeline-axis">
        <component :is="group.key === 'ended' ? 'details' : 'section'" v-for="group in activeGroups" :key="group.key" class="timeline-group">
          <summary v-if="group.key === 'ended'">已结束活动 · {{ group.items.length }} 场</summary>
          <header v-if="group.key === 'upcoming'" class="timeline-group-heading">
            <b>{{ group.title }}</b>
          </header>

          <article v-for="entry in group.items" :key="entry.name" class="timeline-axis-entry" :class="`is-${group.key}`">

            <div class="timeline-event-card">

              <header>
                <div>
                  <span class="event-tags">
                    <span v-if="group.key === 'upcoming'" class="event-state">即将开始</span>
                    <span v-else-if="group.key === 'ended'" class="event-state">已收官</span>
                    <span v-if="supportsTokenLearning(entry)" class="experience-tag" :class="`source-${abacusFor(entry)?.keys_source || 'new'}`">{{ experienceLabel(entry) }}</span>
                  </span>
                  <h5>{{ entry.name }} <small class="event-moment">{{ axisMoment(entry, group.key) }}</small></h5>
                </div>
                <span class="event-range">{{ eventRange(entry) }}</span>
              </header>

              <p v-if="group.key !== 'ended' && !isCurrencyMechanics(entry.budget?.mechanics || '') && eventSummary(entry)" class="event-summary">{{ eventSummary(entry) }}</p>

              <section v-if="group.key === 'ended'" class="event-ended-summary">
                <p v-if="!entry.summary" class="ended-empty">这期狐之助没跑，没留下数据。</p>
                <template v-else>
                  <b v-if="entry.summary.full_clear" class="ended-clear">四座宝库全开 🎉</b>
                  <p v-if="isCurrencyMechanics(entry.summary.mechanics)">跑了 {{ fmt(entry.summary.runs) }} 圈 · 场均 {{ fmt(entry.summary.currency_per_run ?? entry.summary.tama_per_run) }} {{ entry.summary.currency || currencyOf(entry) }} · 共拿 {{ fmt(entry.summary.currency_total ?? entry.summary.total_tama) }} {{ entry.summary.currency || currencyOf(entry) }}</p>
                  <p v-else>打了 {{ fmt(entry.summary.runs) }} 圈 · 场均 {{ fmt(entry.summary.keys_per_run) }} 把 · 共拿 {{ fmt(entry.summary.keys_total) }} 把</p>
                  <p v-if="entry.summary.koban_spent != null">{{ entry.summary.koban_spent > 0 ? `补票花了 ${fmt(entry.summary.koban_spent)} 小判` : '白票全程够用，一个小判没花' }}</p>
                  <small>本期数据已归档，下期复刻狐之助会参考。</small>
                </template>
              </section>

              <section v-if="group.key === 'ongoing' && entry.budget && isCurrencyMechanics(entry.budget.mechanics)" class="event-tama-plan">
                <header class="currency-progress-heading">
                  <b>{{ entry.budget.tama_current == null ? '待第一圈记账' : fmt(entry.budget.tama_current) }} / {{ fmt(entry.budget.tama_target || 300000) }} {{ currencyOf(entry) }}</b>
                  <button type="button" class="target-edit" @click="editTarget(entry)">修改目标</button>
                </header>
                <div v-if="entry.budget.tama_current != null" class="tama-progress"><i :style="{ width: `${tamaProgress(entry)}%` }" /></div>
                <p class="tama-action">{{ tamaRemainingText(entry) }}</p>
                <p v-if="entry.budget.can_finish === false" class="timeline-error">{{ tamaTimeText(entry) }}</p>
                <section v-if="entry.budget.mechanics !== 'raid' && tamaBatchPlan(entry)" class="tama-now-plan" :class="{ waiting: !tamaBatchPlan(entry)!.runs }">
                  <span>
                    <small>现在这一锅</small>
                    <b v-if="tamaBatchPlan(entry)!.runs">先挂 {{ fmt(tamaBatchPlan(entry)!.runs) }} 圈</b>
                    <b v-else>先别开新一锅</b>
                  </span>
                  <p>{{ tamaBatchNote(entry) }}</p>
                  <button v-if="tamaBatchPlan(entry)!.runs" type="button" class="primary" @click="openTamaBatch(entry)">带着 {{ fmt(tamaBatchPlan(entry)!.runs) }} 圈去开工</button>
                </section>
                <details class="tama-estimate-details">
                  <summary>查看估算</summary>
                  <p>{{ tamaEstimateText(entry) }}</p>
                  <p v-if="entry.budget.can_finish !== false && tamaTimeText(entry)">{{ tamaTimeText(entry) }}</p>
                  <p v-if="tamaTicketText(entry)">{{ tamaTicketText(entry) }}</p>
                  <p v-if="entry.budget.tama_observed_at">{{ observedTime(entry.budget.tama_observed_at) }} 收工后读到</p>
                </details>
              </section>

              <details v-if="group.key === 'ongoing' && paceFor(entry)" class="event-pace-calculator"><summary>圈速与时长试算</summary>
                <header>
                  <span><small>按{{ paceSourceLabel(paceFor(entry)!.source) }}</small><b>{{ loopPace(paceFor(entry)!.secondsPerLoop) }}</b></span>
                  <span><small>{{ selectedPaceWindow(entry) === 'event' ? '活动结束前' : `接下来 ${paceDuration(availablePaceSeconds(entry))}` }}</small><strong>{{ selectedPaceWindow(entry) === 'event' ? '纯按时间最多' : '约' }} {{ fmt(possibleLoops(entry)) }} 圈</strong></span>
                </header>
                <div v-if="paceOptions(entry).length > 1" class="pace-source-buttons" aria-label="选择圈速来源">
                  <button v-for="pace in paceOptions(entry)" :key="pace.source" type="button" :class="{ active: paceFor(entry)?.source === pace.source }" @click="setPaceSource(entry, pace.source)">{{ paceSourceLabel(pace.source) }} · {{ loopPace(pace.secondsPerLoop) }}</button>
                </div>
                <div class="pace-window-buttons" aria-label="试算挂机时长">
                  <button type="button" :class="{ active: selectedPaceWindow(entry) === 'event' }" :disabled="!entry.end_at" @click="setPaceWindow(entry, 'event')">活动结束前</button>
                  <button type="button" :class="{ active: selectedPaceWindow(entry) === '1h' }" @click="setPaceWindow(entry, '1h')">1 小时</button>
                  <button type="button" :class="{ active: selectedPaceWindow(entry) === '3h' }" @click="setPaceWindow(entry, '3h')">3 小时</button>
                </div>
                <p>{{ paceSampleDate(paceFor(entry)!.runStartedAt) }} 实测 {{ fmt(paceFor(entry)!.loops) }} 圈 · {{ selectedPaceWindow(entry) === 'event' ? '只算时间，手形另算' : '连续挂机，不预留收尾时间' }}</p>
              </details>

              <div v-if="entry.budget && !isCurrencyMechanics(entry.budget.mechanics) && entry.budget.koban_cost != null" class="event-budget" :class="{ ready: entry.budget.sufficient === true || entry.budget.koban_cost === 0 }">
                <span>
                  <small>{{ entry.budget.sufficient === true || entry.budget.koban_cost === 0 ? '活动预算' : '预算缺口' }}</small>
                  <b>{{ budgetHeadline(entry) }}</b>
                </span>
                <p v-if="budgetText(entry)">{{ budgetText(entry) }}</p>
              </div>

              <div v-if="group.key !== 'ended' && abacusFor(entry)?.goal_mode === 'stock_target'" class="event-stock-target">
                <div v-if="goalFor(entry)" class="stock-goal-linked">
                  <span><small>已立目标</small><b>{{ fmt(goalFor(entry)?.target) }} {{ goalFor(entry)?.resource }}</b></span>
                  <details>
                    <summary>修改</summary>
                    <div><input v-model="targetInputs[entry.name]" type="number" :min="(abacusFor(entry)?.available_now || 0) + 1" max="100000000" step="10000"><button type="button" class="primary" :disabled="goalSaving === entry.name" @click="submitStockGoal(entry)">{{ goalSaving === entry.name ? '保存中……' : '保存' }}</button></div>
                  </details>
                </div>
                <template v-else>
                  <label>收摊时想把小判攒到多少？
                    <input v-model="targetInputs[entry.name]" type="number" :min="(abacusFor(entry)?.available_now || 0) + 1" max="100000000" step="10000" placeholder="例如 1,000,000">
                  </label>
                  <button type="button" class="primary" :disabled="goalSaving === entry.name" @click="submitStockGoal(entry)">{{ goalSaving === entry.name ? '正在立目标……' : '立为目标' }}</button>
                  <p v-if="abacusFor(entry)?.yield_per_floor" class="stock-yield">最近 {{ fmt(abacusFor(entry)?.yield_sessions) }} 次实测，每层约 {{ fmt(abacusFor(entry)?.yield_per_floor) }} 小判。</p>
                  <p v-else class="stock-yield">还没有单层收益样本；先立目标，挖几层后会自动换算。</p>
                </template>
              </div>

              <div v-else-if="group.key !== 'ended' && abacusFor(entry)?.keys_total && abacusFor(entry)?.keys_per_run == null" class="event-estimate">
                <label>你一圈通常拿几把钥匙？
                  <input v-model="estimateInputs[entry.name]" type="number" min="1" max="200" step="1" placeholder="填个估计">
                </label>
                <button type="button" class="primary" :disabled="estimateSaving === entry.name" @click="submitEstimate(entry)">{{ estimateSaving === entry.name ? '计算中……' : '帮我算' }}</button>
              </div>

              <div v-else-if="group.key !== 'ended' && abacusFor(entry)?.koban_cost && entry.budget?.sufficient !== true && entry.budget?.koban_cost !== 0" class="event-actions">
                <span v-if="goalFor(entry)">已加入“当前目标”</span>
                <button v-else type="button" class="primary" :disabled="goalSaving === entry.name" @click="emit('add-goal', abacusFor(entry)!)">{{ goalSaving === entry.name ? '正在立目标……' : '把缺口立成目标' }}</button>
              </div>

              <details v-if="entry.note && entry.note !== abacusFor(entry)?.message">
                <summary>查看活动说明</summary>
                <p>{{ entry.note }}</p>
              </details>
            </div>
          </article>
        </component>
      </div>

      <section v-if="timeline.later.length" class="timeline-later">
        <header><b>稍后</b><span>更远的活动先收成一行</span></header>
        <div class="later-list">
          <article v-for="entry in timeline.later" :key="entry.name">
            <time>{{ shortDate(entry.start_at || entry.start_date) }}</time>
            <b>{{ entry.name }}</b>
            <span>{{ eventRange(entry) }}</span>
          </article>
        </div>
      </section>

      <div v-if="!hasFormalEvents" class="timeline-empty">
        <b>近期没有要开打的活动</b>
        <span>有正式日期后会自动排到这里。</span>
      </div>

      <details v-if="timeline.unverified.length" class="timeline-unverified">
        <summary>
          <span>待确认日程 · {{ timeline.unverified.length }} 条</span>
        </summary>
        <div>
          <a v-for="(candidate, index) in timeline.unverified" :key="`${candidate.name}-${candidate.start_at}-${index}`" :href="candidate.url || undefined" target="_blank" rel="noopener">
            <span><b>{{ candidateName(candidate) }}</b><small>{{ candidate.announcement || '官方公告' }}</small></span>
            <time>{{ candidateRange(candidate) }}</time>
          </a>
        </div>
      </details>
    </div>
  <dialog ref="targetDialog" class="event-target-dialog" aria-label="修改本期目标" @click="($event.target === targetDialog) && targetDialog?.close()">
    <template v-if="targetEntry">
      <h3>{{ targetEntry.name }} · 修改目标</h3>
      <label>目标{{ currencyOf(targetEntry) }}数<input v-model="tamaTargetInputs[targetEntry.name]" type="number" min="1" max="10000000" step="1000"></label>
      <p>只影响本期活动。</p>
      <p v-if="targetError" role="alert">{{ targetError }}</p>
      <button type="button" class="primary" :disabled="targetSaving === targetEntry.name" @click="submitTamaTarget(targetEntry)">{{ targetSaving === targetEntry.name ? '保存中…' : '保存目标' }}</button>
      <button type="button" class="secondary" @click="targetDialog?.close()">关闭</button>
    </template>
  </dialog>
  </section>
</template>

<style scoped>
.event-target-dialog { width: min(420px, calc(100vw - 32px)); box-sizing: border-box; padding: 24px; border: 1px solid var(--paper-line); border-radius: 12px; background: var(--paper-card); color: var(--ink); }
.event-target-dialog::backdrop { background: rgb(0 0 0 / 38%); }
.event-target-dialog label { display: grid; gap: 8px; }
.event-target-dialog input { width: 100%; box-sizing: border-box; }
.event-target-dialog p { color: var(--ink-dim); font-size: 12px; }
.event-moment { color: var(--ink-dim); font-size: 12px; font-weight: 400; margin-left: 10px; }
.target-edit { background: transparent; border: 0; color: var(--fox-gold-deep); cursor: pointer; white-space: nowrap; font-size: 12px; }
.tama-estimate-details p { font-size: 12px; color: var(--ink-dim); line-height: 1.7; }

.event-timeline-card { padding: 16px 18px; background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 12px; }
.timeline-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.timeline-heading small { color: var(--fox-gold-deep); font-size: 11px; font-weight: 700; letter-spacing: .08em; }
.timeline-heading h4 { margin: 2px 0 0; font-size: 16px; }
.timeline-stale { flex: none; padding: 3px 8px; color: #936429; background: var(--fox-gold-pale); border-radius: 999px; font-size: 11px; }
.timeline-error { margin: 12px 0 0; padding: 10px 12px; color: #9f3d28; background: #f9e6df; border-radius: 8px; font-size: 13px; }
.timeline-axis { display: grid; gap: 16px; margin-top: 16px; }
.timeline-group { display: grid; gap: 9px; }
.timeline-group-heading { display: flex; align-items: baseline; gap: 8px; padding-left: 0; }
.timeline-group-heading b { font-size: 13px; }
.timeline-group-heading span { color: var(--ink-dim); font-size: 11px; }
.timeline-axis-entry { display: grid; grid-template-columns: minmax(0, 1fr); align-items: stretch; }
.timeline-axis-entry > time { padding: 11px 8px 0 0; color: var(--ink-dim); font-size: 11px; font-weight: 700; text-align: right; }
.timeline-rail { position: relative; display: flex; justify-content: center; }
.timeline-rail::after { position: absolute; top: 0; bottom: -10px; width: 1px; background: var(--paper-line); content: ''; }
.timeline-rail i { position: relative; z-index: 1; width: 9px; height: 9px; margin-top: 14px; background: var(--paper-card); border: 2px solid var(--fox-gold); border-radius: 50%; }
.is-ongoing .timeline-rail i { background: #5b813f; border-color: #5b813f; box-shadow: 0 0 0 4px color-mix(in srgb, #5b813f 14%, transparent); }
.timeline-event-card { min-width: 0; padding: 13px 15px; background: transparent; border: 0; border-radius: 0; }
.is-ongoing .timeline-event-card { border-left: 0; }
.is-upcoming .timeline-event-card { border-left: 4px solid var(--fox-gold); }
.timeline-event-card > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.timeline-event-card > header > div { display: grid; gap: 2px; min-width: 0; }
.event-tags { display: flex; align-items: center; gap: 6px; }
.event-state { color: var(--fox-gold-deep); font-size: 10px; font-weight: 700; letter-spacing: .06em; }
.is-ongoing .event-state { color: #4d7137; }
.experience-tag { padding: 1px 6px; color: var(--ink-dim); background: var(--paper-panel); border-radius: 999px; font-size: 10px; font-weight: 700; letter-spacing: .03em; }
.experience-tag.source-measured { color: #426b36; background: color-mix(in srgb, #dcebd6 76%, var(--paper-card)); }
.experience-tag.source-history { color: #805f20; background: var(--fox-gold-pale); }
.experience-tag.source-estimate { color: #765e50; background: color-mix(in srgb, #e9ded4 74%, var(--paper-card)); }
.timeline-event-card h5 { margin: 0; font-size: 16px; }
.event-range { flex: none; color: var(--ink-dim); font-size: 11px; white-space: nowrap; }
.event-mobile-moment { display: none; }
.event-summary { margin: 9px 0 0; color: var(--ink-dim); font-size: 13px; line-height: 1.55; }
.event-tama-plan { display: grid; gap: 9px; margin-top: 11px; padding: 11px 12px; background: transparent; border-left: 0; border-radius: 8px; }
.event-tama-plan > header { display: flex; align-items: end; justify-content: space-between; gap: 14px; }
.event-tama-plan > header span { display: grid; gap: 1px; }
.event-tama-plan > header span:last-child { text-align: right; }
.event-tama-plan small { color: #426b36; font-size: 10px; font-weight: 700; }
.event-tama-plan b { font-size: 18px; font-variant-numeric: tabular-nums; }
.event-tama-plan > p { margin: 0; color: var(--ink-dim); font-size: 11px; }
.event-tama-plan .tama-action { color: var(--ink); font-size: 12px; font-weight: 600; }
.tama-target-editor { margin-top: 0 !important; padding-top: 7px; border-top: 1px dashed color-mix(in srgb, #5b813f 32%, var(--paper-line)); }
.tama-target-editor > div { display: flex; gap: 7px; margin-top: 7px; }
.tama-target-editor input { min-width: 0; width: min(220px, 100%); background: var(--paper-card); }
.tama-target-editor > small { display: block; margin-top: 6px; color: var(--ink-dim); font-weight: 400; }
.tama-progress { height: 6px; overflow: hidden; background: color-mix(in srgb, var(--paper-line) 70%, transparent); border-radius: 999px; }
.tama-progress i { display: block; height: 100%; background: #5b813f; border-radius: inherit; transition: width .25s ease; }
.tama-now-plan { display: grid; grid-template-columns: minmax(130px, auto) minmax(180px, 1fr) auto; align-items: center; gap: 8px 16px; padding: 12px; background: color-mix(in srgb, var(--fox-gold-pale) 82%, var(--paper-card)); border: 1px solid color-mix(in srgb, var(--fox-gold) 58%, var(--paper-line)); border-radius: 8px; }
.tama-now-plan > span { display: grid; gap: 1px; }
.tama-now-plan small { color: var(--fox-gold-deep); font-size: 10px; font-weight: 700; }
.tama-now-plan b { color: var(--ink); font-size: 18px; }
.tama-now-plan p { margin: 0; color: var(--ink-dim); font-size: 11px; }
.tama-now-plan button { white-space: nowrap; }
.tama-now-plan.waiting { grid-template-columns: minmax(130px, auto) 1fr; background: color-mix(in srgb, var(--paper) 72%, var(--paper-card)); border-style: dashed; }
.event-pace-calculator { gap: 9px; margin-top: 11px; padding: 11px; background: color-mix(in srgb, var(--fox-gold-pale) 70%, var(--paper-card)); border: 1px solid color-mix(in srgb, var(--fox-gold) 38%, var(--paper-line)); border-radius: 8px; }
.event-pace-calculator > header { display: flex; align-items: end; justify-content: space-between; gap: 14px; }
.event-pace-calculator > header > span { display: grid; gap: 1px; }
.event-pace-calculator > header > span:last-child { text-align: right; }
.event-pace-calculator small { color: var(--ink-dim); font-size: 10px; }
.event-pace-calculator b { font-size: 14px; }
.event-pace-calculator strong { color: var(--fox-gold-deep); font-size: 18px; }
.pace-window-buttons { display: flex; flex-wrap: wrap; gap: 6px; }
.pace-source-buttons { display: flex; flex-wrap: wrap; gap: 6px; padding-bottom: 8px; border-bottom: 1px dashed var(--paper-line); }
.pace-window-buttons button, .pace-source-buttons button { padding: 5px 10px; color: var(--ink-dim); background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 999px; font-size: 11px; cursor: pointer; }
.pace-window-buttons button.active, .pace-source-buttons button.active { color: var(--ink); background: var(--fox-gold-pale); border-color: var(--fox-gold); font-weight: 700; }
.pace-window-buttons button:disabled { cursor: not-allowed; opacity: .45; }
.event-pace-calculator > p { margin: 0; color: var(--ink-dim); font-size: 11px; }
.event-budget { display: flex; align-items: center; gap: 14px; margin-top: 11px; padding: 9px 11px; background: color-mix(in srgb, #f4dfd7 68%, var(--paper-card)); border-radius: 8px; }
.event-budget.ready { background: transparent; }
.event-budget > span { display: grid; flex: 0 0 auto; gap: 1px; min-width: 92px; }
.event-budget small { color: var(--ink-dim); font-size: 10px; }
.event-budget b { color: #9f3d28; font-size: 14px; }
.event-budget.ready b { color: #426b36; }
.event-budget p { margin: 0; color: var(--ink-dim); font-size: 12px; }
.event-estimate { display: flex; align-items: flex-end; gap: 8px; margin-top: 11px; padding: 10px 11px; background: var(--fox-gold-pale); border-radius: 8px; }
.event-ended-summary { margin-top: 11px; padding: 10px 11px; background: transparent; border-radius: 8px; }
.event-ended-summary p { margin: 4px 0 0; color: var(--ink-dim); font-size: 12px; }
.event-ended-summary .ended-clear { color: #426b36; font-size: 14px; }
.event-ended-summary .ended-empty { margin: 0; }
.event-ended-summary small { display: block; margin-top: 6px; color: var(--ink-dim); font-size: 10px; }
.event-estimate label { display: grid; flex: 1 1 auto; gap: 4px; font-size: 12px; }
.event-estimate input { width: min(190px, 100%); background: var(--paper-card); }
.event-stock-target { display: grid; grid-template-columns: minmax(180px, 1fr) auto; align-items: end; gap: 7px 9px; margin-top: 11px; padding: 11px; background: color-mix(in srgb, #dcebd6 62%, var(--paper-card)); border-radius: 8px; }
.event-stock-target label { display: grid; gap: 4px; color: var(--ink); font-size: 12px; font-weight: 700; }
.event-stock-target input { width: min(260px, 100%); background: var(--paper-card); font-weight: 400; }
.event-stock-target .stock-yield { grid-column: 1 / -1; color: var(--ink-dim); font-size: 11px; }
.event-stock-target .stock-yield { margin: 0; }
.stock-goal-linked { display: flex; grid-column: 1 / -1; align-items: center; justify-content: space-between; gap: 12px; }
.stock-goal-linked > span { display: grid; gap: 1px; }
.stock-goal-linked small { color: #426b36; font-size: 10px; font-weight: 700; }
.stock-goal-linked details { margin: 0; }
.stock-goal-linked details[open] { width: min(330px, 100%); }
.stock-goal-linked details > div { display: flex; gap: 7px; margin-top: 6px; }
.stock-goal-linked input { min-width: 0; }
.event-actions { display: flex; align-items: center; justify-content: flex-end; gap: 10px; margin-top: 11px; }
.event-actions span { color: var(--ink-dim); font-size: 12px; }
.timeline-event-card details { margin-top: 9px; color: var(--ink-dim); font-size: 12px; }
.timeline-event-card summary { color: var(--fox-gold-deep); cursor: pointer; }
.timeline-event-card details p { margin: 6px 0 0; line-height: 1.6; }
.timeline-later { margin-top: 16px; padding-top: 13px; border-top: 1px dashed var(--paper-line); }
.timeline-later > header { display: flex; align-items: baseline; gap: 8px; margin-bottom: 7px; }
.timeline-later > header b { font-size: 13px; }
.timeline-later > header span { color: var(--ink-dim); font-size: 11px; }
.later-list { display: grid; }
.later-list article { display: grid; grid-template-columns: 72px minmax(120px, .7fr) minmax(180px, 1fr); gap: 10px; padding: 8px 10px; border-top: 1px solid color-mix(in srgb, var(--paper-line) 65%, transparent); font-size: 12px; }
.later-list article:first-child { border-top: 0; }
.later-list time, .later-list span { color: var(--ink-dim); }
.timeline-empty { display: grid; gap: 3px; margin-top: 14px; padding: 16px; color: var(--ink-dim); background: var(--paper); border: 1px dashed var(--paper-line); border-radius: 9px; font-size: 13px; }
.timeline-empty b { color: var(--ink); }
.timeline-unverified { margin-top: 14px; padding-top: 12px; border-top: 1px dashed var(--paper-line); }
.timeline-unverified > summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: var(--ink); cursor: pointer; list-style: none; }
.timeline-unverified > summary::-webkit-details-marker { display: none; }
.timeline-unverified > summary > span { display: grid; gap: 2px; }
.timeline-unverified summary small { color: var(--ink-dim); font-size: 11px; font-weight: 400; }
.timeline-unverified summary em { display: grid; place-items: center; width: 24px; height: 24px; color: var(--fox-gold-deep); background: var(--fox-gold-pale); border-radius: 50%; font-size: 11px; font-style: normal; }
.timeline-unverified > div { display: grid; gap: 6px; margin-top: 9px; }
.timeline-unverified a { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 9px 10px; color: var(--ink); background: var(--paper); border-radius: 8px; text-decoration: none; }
.timeline-unverified a:hover { background: var(--fox-gold-pale); }
.timeline-unverified a > span { display: grid; gap: 1px; min-width: 0; }
.timeline-unverified a small, .timeline-unverified a time { color: var(--ink-dim); font-size: 11px; }
.timeline-unverified a time { flex: none; }
@media (max-width: 700px) {
  .event-timeline-card { padding: 14px; }
  .timeline-group-heading { padding-left: 92px; }
  .timeline-axis-entry { grid-template-columns: 67px 16px minmax(0, 1fr); }
  .timeline-event-card > header { flex-direction: column; gap: 5px; }
  .event-range { white-space: normal; }
  .later-list article { grid-template-columns: 62px minmax(0, 1fr); }
  .later-list article span { grid-column: 2; }
}
@media (max-width: 520px) {
  .timeline-heading { flex-direction: column; gap: 7px; }
  .timeline-axis { gap: 14px; }
  .timeline-group-heading { padding-left: 0; }
  .timeline-axis-entry { grid-template-columns: minmax(0, 1fr); }
  .timeline-axis-entry > time, .timeline-rail { display: none; }
  .event-mobile-moment { display: block; margin-bottom: 5px; color: var(--fox-gold-deep); font-size: 11px; font-weight: 700; }
  .is-ongoing .event-mobile-moment { color: #4d7137; }
  .timeline-event-card { padding: 12px; }
  .event-budget, .event-estimate { align-items: stretch; flex-direction: column; }
  .event-tama-plan > header { align-items: flex-start; flex-direction: column; gap: 7px; }
  .event-tama-plan > header span:last-child { text-align: left; }
  .tama-target-editor > div { align-items: stretch; flex-direction: column; }
  .tama-target-editor input, .tama-target-editor button { width: 100%; max-width: none; }
  .tama-now-plan, .tama-now-plan.waiting { grid-template-columns: 1fr; align-items: stretch; }
  .tama-now-plan button { width: 100%; white-space: normal; }
  .event-pace-calculator > header { align-items: flex-start; flex-direction: column; gap: 7px; }
  .event-pace-calculator > header > span:last-child { text-align: left; }
  .event-estimate input, .event-estimate button, .event-stock-target input, .event-stock-target button { width: 100%; max-width: none; }
  .event-stock-target { grid-template-columns: 1fr; }
  .event-stock-target .stock-yield { grid-column: 1; }
  .stock-goal-linked { align-items: flex-start; flex-direction: column; }
  .stock-goal-linked details, .stock-goal-linked details[open] { width: 100%; }
  .stock-goal-linked details > div { align-items: stretch; flex-direction: column; }
  .event-actions { align-items: stretch; flex-direction: column; }
  .event-actions button { width: 100%; }
  .timeline-unverified a { align-items: flex-start; flex-direction: column; gap: 4px; }
}
</style>
