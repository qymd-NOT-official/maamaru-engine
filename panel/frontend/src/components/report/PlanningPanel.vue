<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { api } from '../../api'
import type { ActivityPace, DayTimeline as DayTimelineData, EventAbacus, EventTimelineReport, ManualSession, PlanningGoalAdvice, PlanningReport, ScriptParams } from '../../types'
import { resourceNames } from './reportModel'
// 氪金货币不立目标，下拉选项里拿掉甲州金；账本展示那边 resourceNames 照旧
const goalResources = resourceNames.filter(name => name !== '甲州金')
// 异去碎片清单由服务端途径卡给出（数据卡没收录就是空，不硬编）
const fragmentNames = computed(() => Object.keys(planning.value?.fragments || {}))
import EventPointsHistory from './EventPointsHistory.vue'
import EventTimeline from './EventTimeline.vue'
import ResourceGoalGuide from './ResourceGoalGuide.vue'
import FragmentGoalGuide from './FragmentGoalGuide.vue'
import GameplayPlanner from './GameplayPlanner.vue'
import PlanningOverview from './PlanningOverview.vue'
import DayTimeline from '../DayTimeline.vue'

const props = defineProps<{ recoveryRunId?: string; server?: string }>()
const isJp = computed(() => props.server === 'jp')
const emit = defineEmits<{
  gameplaySettingsSaved: [script: string, params: ScriptParams]
  goalSaved: []
  openExpedition: []
  openActivity: [script: 'hanafuda' | 'raid', loops: number]
}>()
const planning = ref<PlanningReport | null>(null)
const timeline = ref<EventTimelineReport | null>(null)
const dayTimeline = ref<DayTimelineData | null>(null)
const dayTimelineRequest = ref(0)
const focusRefresh = ref(0)
const focusSaving = ref(false)
async function changeResourceFocus(resource: string) {
  focusSaving.value = true
  try {
    await api.setExpeditionResourceFocus(resource)
    focusRefresh.value++
  } catch (err) {
    error.value = err instanceof Error ? err.message : '关注项保存失败'
  } finally {
    focusSaving.value = false
  }
}
const loading = ref(false)
let refreshingPlanning = false
watch(dayTimeline, async snapshot => {
  if (!snapshot || !planning.value || loading.value || refreshingPlanning) return
  refreshingPlanning = true
  try {
    const updated = await api.planning()
    planning.value = updated
  } catch {
    // 保留上一份已读到的家底；下一次时间表刷新时再读取。
  } finally {
    refreshingPlanning = false
  }
})
const error = ref('')
const timelineError = ref('')
const goalNotice = ref('')
const activityPaces = ref<Record<string, ActivityPace[]>>({})
const budgetGoals = computed(() => (planning.value?.goals || []).filter(goal => goal.kind === 'event' && goal.goal_mode === 'budget'))
const customGoals = computed(() => (planning.value?.goals || []).filter(goal => goal.kind !== 'event'))

const formOpen = ref(false)
const saving = ref(false)
const form = ref({ kind: 'resource' as 'resource' | 'fragment', goal_mode: 'amount_target' as 'amount_target' | 'deadline_target', resource: '小判', fragment: '', target: 100000, deadline: '', note: '' })
watch(() => form.value.kind, (kind) => {
  if (kind === 'fragment' && !form.value.fragment) {
    form.value.fragment = fragmentNames.value[0] || ''
  }
})

function applyTimingFallback(report: PlanningReport, runs: any[]) {
  // 兼容已经启动、暂时不能为了热加载而重启的旧后端。
  // 新接口给出结构化工时时绝不覆盖；这里只复用成绩单现成的最近层速。
  const cutoff = Date.now() / 1000 - 14 * 86400
  const latest = runs.find(run => (
    run.script === 'osaka'
    && Number(run.started_at) >= cutoff
    && Number(run.loops) > 0
    && Number(run.average_loop_seconds) > 0
  ))
  if (!latest) return
  const secondsPerFloor = Number(latest.average_loop_seconds)
  for (const goal of report.goals) {
    if (['done', 'expired'].includes(goal.status) || goal.goal_mode !== 'stock_target' || goal.estimated_seconds != null
        || goal.floors_needed == null || !goal.deadline_at) continue
    const remainingSeconds = Math.max(0, Math.round(
      (new Date(goal.deadline_at).getTime() - Date.now()) / 1000))
    const estimatedSeconds = Math.ceil(goal.floors_needed * secondsPerFloor)
    goal.seconds_per_floor = secondsPerFloor
    goal.speed_sample_floors = Number(latest.loops)
    goal.estimated_seconds = estimatedSeconds
    goal.remaining_seconds = remainingSeconds
    goal.time_margin_seconds = remainingSeconds - estimatedSeconds
    goal.can_finish = goal.time_margin_seconds >= 0
  }
}

function collectActivityPaces(runs: any[], manualSessions: ManualSession[]) {
  const definitions = [
    { event: '大阪城', script: 'osaka' },
    { event: '联队战', script: 'raid' },
    { event: '江户城潜入调查', script: 'edocastle' },
    { event: '秘宝之里', script: 'hanafuda' },
  ]
  const result: Record<string, ActivityPace[]> = {}
  for (const definition of definitions) {
    const latestMachine = runs.find(run => (
      run.script === definition.script
      && Number(run.loops) > 0
      && Number(run.average_loop_seconds) > 0
    ))
    const latestManual = manualSessions.find(item => item.script === definition.script)
    const paces: ActivityPace[] = []
    if (latestMachine) paces.push({
      source: 'maamaru', secondsPerLoop: Number(latestMachine.average_loop_seconds),
      loops: Number(latestMachine.loops), runStartedAt: Number(latestMachine.started_at),
    })
    if (latestManual) paces.push({
      source: 'manual', secondsPerLoop: Number(latestManual.average_loop_seconds),
      loops: Number(latestManual.loops), runStartedAt: Number(latestManual.started_at),
    })
    if (paces.length) result[definition.event] = paces
  }
  activityPaces.value = result
}

function localToday() {
  const now = new Date()
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10)
}

function fmt(value: number | null | undefined) {
  return value == null ? '—' : Math.round(value).toLocaleString()
}

function goalDeadline(goal: PlanningGoalAdvice) {
  if (goal.goal_mode === 'amount_target') return goal.estimated_deadline || '待估算'
  if (!goal.deadline_at) return goal.deadline || '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
    hour12: false, timeZone: 'Asia/Shanghai',
  }).format(new Date(goal.deadline_at))
}

const statusLabel: Record<string, string> = {
  done: '已达成',
  on_track: '来得及',
  behind: '要加把劲',
  active: '进行中',
  expired: '已到期',
  unknown: '数据不足',
}

function goalStatusLabel(goal: PlanningGoalAdvice) {
  if (goal.kind === 'event' && goal.goal_mode === 'budget') {
    if (goal.status === 'expired') return '已收摊'
    if (goal.current != null && goal.target != null && goal.current >= goal.target) return '已备齐'
    return goal.shortfall != null ? '还需准备' : '待看家底'
  }
  if (goal.status === 'done' || goal.status === 'expired') return statusLabel[goal.status]
  if (goal.goal_mode === 'stock_target' && goal.can_finish === false) return '来不及'
  if (goal.goal_mode === 'stock_target' && goal.can_finish === true) return '来得及'
  return statusLabel[goal.status] || goal.status
}

function goalHeadline(goal: PlanningGoalAdvice) {
  if (isJp.value && goal.goal_mode === 'deadline_target') return '已记下日期；到期预测待适配'
  if (isJp.value && goal.status !== 'done' && goal.goal_mode === 'amount_target') {
    return goal.current == null ? '家底尚未读取' : `还差 ${fmt(Math.max(0, (goal.target || 0) - goal.current))} ${goal.resource}`
  }
  if (goal.goal_mode === 'deadline_target') {
    return goal.projected == null ? '还缺一些库存记录' : `预计到期有 ${fmt(goal.projected)} ${goal.resource}`
  }
  if (goal.goal_mode === 'amount_target' && goal.status === 'active') return `预计 ${goal.estimated_deadline || '稍后'} 攒够`
  if (goal.status === 'done') return `已经攒够 ${fmt(goal.target)} ${goal.resource}`
  if (goal.status === 'on_track') return '照现在的速度来得及'
  if (goal.status === 'expired') return '这个目标已经到期'
  if (goal.status === 'unknown' && goal.goal_mode === 'stock_target' && goal.shortfall != null) return `还差 ${fmt(goal.shortfall)} ${goal.resource}`
  if (goal.status === 'unknown') return '还缺一些库存记录'
  if (goal.status === 'active' && goal.shortfall != null) return `还差 ${fmt(goal.shortfall)} ${goal.resource}`
  if (goal.shortfall != null) return `还差 ${fmt(goal.shortfall)} ${goal.resource}`
  return '需要再加把劲'
}

function goalProgress(goal: PlanningGoalAdvice) {
  if (goal.current == null || goal.target == null || goal.target <= 0) return 0
  return Math.min(100, Math.max(0, goal.current / goal.target * 100))
}

function fragmentGuide(goal: PlanningGoalAdvice) {
  return planning.value?.fragments?.[goal.fragment || ''] ?? null
}

function acquisitionGuide(goal: PlanningGoalAdvice) {
  return planning.value?.acquisition?.[goal.resource] ?? null
}

function durationHours(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return ''
  const minutes = Math.max(0, Math.round(seconds / 60))
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  if (!hours) return `${rest} 分钟`
  return `${hours} 小时${rest ? ` ${rest} 分` : ''}`
}

function floorPace(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return ''
  const value = Math.max(0, Math.round(seconds))
  return `${Math.floor(value / 60)}分${String(value % 60).padStart(2, '0')}秒`
}

function goalProgressMeta(goal: PlanningGoalAdvice) {
  if (isJp.value) return goal.goal_mode === 'deadline_target' ? '已记下日期，预测待适配' : ''
  if (goal.status === 'done') return ''
  if (goal.kind === 'fragment') return ''  // 碎片目标的指引全在 FragmentGoalGuide 卡里
  if (goal.status === 'expired') return '已到期'
  const remaining = goal.goal_mode === 'stock_target' && goal.remaining_seconds != null
    ? `距收摊 ${durationHours(goal.remaining_seconds)}`
    : goal.goal_mode === 'amount_target'
      ? `约 ${Math.max(0, goal.days_left || 0)} 天后`
      : `剩 ${Math.max(0, goal.days_left || 0)} 天`
  if (goal.goal_mode === 'deadline_target') {
    return goal.projected == null ? `${remaining} · 待估算` : `到期预计 ${fmt(goal.projected)} ${goal.resource}`
  }
  if (goal.goal_mode === 'amount_target') return remaining
  if (goal.goal_mode === 'stock_target') {
    return `${remaining} · ${goal.floors_needed == null ? '待实测' : `还需约 ${fmt(goal.floors_needed)} 层`}`
  }
  return goal.projected == null ? remaining : `${remaining} · 预计 ${fmt(goal.projected)}`
}

function goalAction(goal: PlanningGoalAdvice) {
  if (isJp.value) return goal.status === 'done' ? '资源目标已达成；游戏执行仍待适配。' : ''
  if (goal.goal_mode === 'stock_target' && goal.status === 'active' && goal.floors_needed != null) {
    if (goal.estimated_seconds != null && goal.remaining_seconds != null && goal.can_finish != null) {
      const margin = durationHours(Math.abs(goal.time_margin_seconds || 0))
      const verdict = goal.can_finish
        ? `照当前速度能打完，约余 ${margin}`
        : `照当前速度来不及，约差 ${margin}`
      return `按最近一轮 ${floorPace(goal.seconds_per_floor)}/层，预计还要打 ${durationHours(goal.estimated_seconds)}；离收摊还有 ${durationHours(goal.remaining_seconds)}，${verdict}。`
    }
    const remaining = goal.remaining_seconds == null ? '' : `，离收摊还有 ${durationHours(goal.remaining_seconds)}`
    return `结束前还要挖约 ${fmt(goal.floors_needed)} 层${remaining}；最近还没有可用层速，暂时算不出要挂多久。`
  }
  if (goal.goal_mode === 'stock_target' && goal.status === 'unknown') return '再挖几层，单层收益稳定后会自动换算。'
  if (goal.status === 'behind' && goal.extra_daily != null) {
    const floors = goal.resource === '小判' && goal.extra_floors != null ? `，约合每天多挖 ${fmt(goal.extra_floors)} 层大阪城` : ''
    return `接下来每天还需多攒 ${fmt(goal.extra_daily)} ${goal.resource}${floors}`
  }
  if (goal.status === 'on_track') return '保持最近的进账速度即可。'
  if (goal.status === 'done') return '目标已经完成，可以放心开打。'
  return ''
}

const estimateSaving = ref('')
const abacusGoalSaving = ref('')
const tamaTargetSaving = ref('')

async function saveEstimate(event: string, value: number) {
  if (!Number.isFinite(value) || value <= 0) {
    error.value = '先填一个大于 0 的场均钥匙数。'
    return
  }
  estimateSaving.value = event
  try {
    await api.saveEventEstimate(event, value)
    await load()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '场均预估保存失败' }
  finally { estimateSaving.value = '' }
}

async function goalFromAbacus(abacus: EventAbacus) {
  if (!abacus.koban_cost) return
  goalNotice.value = ''
  abacusGoalSaving.value = abacus.event
  try {
    await api.addEventGoal(abacus.event)
    await load()
    goalNotice.value = `「${abacus.event}」目标已保存。`
    emit('goalSaved')
    await scrollToElement('.planning-success')
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '目标保存失败' }
  finally { abacusGoalSaving.value = '' }
}

async function goalFromStockTarget(abacus: EventAbacus, target: number) {
  goalNotice.value = ''
  if (!Number.isFinite(target) || target <= 0) {
    error.value = '先填一个想攒到的小判数。'
    return
  }
  if (abacus.available_now != null && target <= abacus.available_now) {
    error.value = '目标得比现有小判多一点。'
    return
  }
  abacusGoalSaving.value = abacus.event
  try {
    await api.addEventGoal(abacus.event, target)
    await load()
    goalNotice.value = `「${abacus.event}」目标已保存。`
    emit('goalSaved')
    await scrollToElement('.planning-success')
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '目标保存失败' }
  finally { abacusGoalSaving.value = '' }
}

async function saveTamaTarget(event: string, target: number) {
  goalNotice.value = ''
  const currency = planning.value?.events
    ?.find(item => item.event === event)?.currency || '玉'
  if (!Number.isInteger(target) || target < 1 || target > 10_000_000) {
    error.value = `目标${currency}数请填 1 到 10,000,000 之间的整数。`
    return
  }
  error.value = ''
  tamaTargetSaving.value = event
  try {
    await api.saveEventTarget(event, target)
    await load()
    goalNotice.value = `「${event}」本期目标已改为 ${target.toLocaleString()} ${currency}。`
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '活动目标保存失败' }
  finally { tamaTargetSaving.value = '' }
}


async function gameplayGoalSaved() {
  await load()
  goalNotice.value = '「异去」活动预算已保存。'
  emit('goalSaved')
  await scrollToElement('.planning-success')
}

async function load() {
  loading.value = true
  if (isJp.value) {
    try { planning.value = await api.planning('jp'); error.value = '' }
    catch (cause) { error.value = cause instanceof Error ? cause.message : '规划读取失败' }
    finally { loading.value = false }
    return
  }
  try {
    const [planningResult, timelineResult, runsResult, manualResult] = await Promise.allSettled([
      api.planning(), api.eventsTimeline(), api.dataRuns(100), api.manualSessions(1000),
    ])
    if (planningResult.status === 'fulfilled') {
      if (runsResult.status === 'fulfilled') {
        applyTimingFallback(planningResult.value, runsResult.value.items)
        collectActivityPaces(
          runsResult.value.items,
          manualResult.status === 'fulfilled' ? manualResult.value.items : [],
        )
      }
      planning.value = planningResult.value
      error.value = ''
    } else {
      error.value = planningResult.reason instanceof Error ? planningResult.reason.message : '规划建议读取失败'
    }
    if (timelineResult.status === 'fulfilled') {
      timeline.value = timelineResult.value
      timelineError.value = ''
    } else {
      timelineError.value = timelineResult.reason instanceof Error ? timelineResult.reason.message : '近期活动读取失败'
    }
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '规划建议读取失败' }
  finally { loading.value = false }
}

async function scrollToElement(selector: string) {
  await nextTick()
  const element = document.querySelector(selector)
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  element?.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' })
}

function openRaidRecommendation() {
  dayTimelineRequest.value += 1
}

async function openCustomForm() {
  goalNotice.value = ''
  formOpen.value = true
  form.value = { ...form.value, note: '' }
  await scrollToElement('.planning-form')
}

async function saveGoal() {
  goalNotice.value = ''
  saving.value = true
  try {
    await api.addPlanningGoal(form.value.kind === 'fragment' ? {
      kind: 'fragment',
      fragment: form.value.fragment,
      target: Number(form.value.target),
      note: form.value.note,
    } : {
      goal_mode: form.value.goal_mode,
      resource: form.value.resource,
      ...(form.value.goal_mode === 'amount_target'
        ? { target: Number(form.value.target) }
        : { deadline: form.value.deadline }),
      note: form.value.note,
    }, props.server)
    formOpen.value = false
    form.value = { kind: 'resource', goal_mode: 'amount_target', resource: '小判', fragment: '', target: 100000, deadline: '', note: '' }
    await load()
    goalNotice.value = '目标已保存。'
    emit('goalSaved')
    await scrollToElement('.planning-success')
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '目标保存失败' }
  finally { saving.value = false }
}

async function removeGoal(id: number) {
  try {
    await api.deletePlanningGoal(id, props.server)
    await load()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '目标删除失败' }
}

defineExpose({ openCustomForm })
onMounted(load)
</script>

<template>
  <section class="planning-panel" :class="{ loading }">
    <p v-if="error" class="planning-error">{{ error }}</p>
    <p v-if="goalNotice" class="planning-success" role="status">✓ {{ goalNotice }}</p>

    <p v-if="isJp" class="planning-success">日服目标独立保存。活动日程、玩法试算、自动排班与执行待适配；暂不预测达成日期。</p>
    <DayTimeline :daily-settings="!isJp" v-if="!isJp" :recovery-run-id="props.recoveryRunId" :refresh-request="focusRefresh" :adopt-recommendation-request="dayTimelineRequest" collapsible @timeline-updated="dayTimeline = $event" @gameplay-settings-saved="(script, params) => emit('gameplaySettingsSaved', script, params)" @open-expedition="emit('openExpedition')" />
    <PlanningOverview v-if="planning && !isJp" :planning="planning" :budgets="budgetGoals" :resource-focus="dayTimeline?.expedition_help.resource_focus" :suggested-resource="dayTimeline?.expedition_help.suggested_resource" :focus-saving="focusSaving" @change-focus="changeResourceFocus" @open-expedition="emit('openExpedition')" />
    <EventTimeline v-if="!isJp"
      id="event-timeline"
      :timeline="timeline"
      :abacuses="planning?.events || []"
      :goals="planning?.goals || []"
      :loading="loading"
      :error="timelineError"
      :estimate-saving="estimateSaving"
      :goal-saving="abacusGoalSaving"
      :target-saving="tamaTargetSaving"
      :target-error="error"
      :activity-paces="activityPaces"
      :day-timeline="dayTimeline"
      @save-estimate="saveEstimate"
      @save-tama-target="saveTamaTarget"
      @add-goal="goalFromAbacus"
      @add-stock-goal="goalFromStockTarget"
      @open-activity="(script, loops) => emit('openActivity', script, loops)"
      @open-raid-recommendation="openRaidRecommendation"
    />

    <EventPointsHistory v-if="!isJp" />
    <section v-if="isJp && planning" class="planning-form" aria-label="日服规划家底"><h4>最近读取的家底</h4><p v-for="name in goalResources" :key="name">{{ name }}：{{ planning.current?.[name] == null ? '未读取' : fmt(planning.current?.[name]) }}</p></section>
    <header class="planning-toolbar">
      <div><h3>自定目标</h3><span v-if="customGoals.length">{{ customGoals.length }} 个</span></div>
      <button v-if="!formOpen" type="button" class="secondary" @click="openCustomForm">＋ 添加</button>
    </header>

    <form v-if="formOpen" class="planning-form" @submit.prevent="saveGoal">
      <header>
        <div><h4>自定目标</h4><p>{{ isJp ? '记下资源目标；日服速度预测待适配。' : '选一个真正想盯住的结果，另一项交给狐之助估算。' }}</p></div>
        <button type="button" class="planning-close" aria-label="关闭目标表单" @click="formOpen = false">×</button>
      </header>
      <div class="planning-form-fields">
        <label>目标类型
          <select v-model="form.kind">
            <option value="resource">攒资源</option>
            <option v-if="!isJp" value="fragment">集碎片（异去）</option>
          </select>
        </label>
        <label v-if="form.kind === 'resource'">目标看什么
          <select v-model="form.goal_mode">
            <option value="amount_target">攒到多少</option>
            <option value="deadline_target">到哪一天</option>
          </select>
        </label>
        <label v-if="form.kind === 'resource'">攒什么
          <select v-model="form.resource">
            <option v-for="name in goalResources" :key="name" :value="name">{{ name }}</option>
          </select>
        </label>
        <label v-else>集哪种碎片
          <select v-model="form.fragment" required>
            <option v-for="name in fragmentNames" :key="name" :value="name">{{ name }}</option>
          </select>
        </label>
        <label v-if="form.goal_mode === 'amount_target'">{{ form.kind === 'fragment' ? '想集几个' : '想攒到多少' }}
          <input v-model.number="form.target" type="number" min="1" :step="form.kind === 'fragment' ? 1 : 1000" required>
        </label>
        <label v-else>想看到哪天
          <input v-model="form.deadline" type="date" :min="localToday()" required>
        </label>
        <label>备注
          <input v-model="form.note" maxlength="50" placeholder="可不填">
        </label>
      </div>
      <div class="planning-form-actions">
        <button type="submit" class="primary" :disabled="saving">{{ saving ? '记账中……' : '立下目标' }}</button>
        <button type="button" class="secondary" @click="formOpen = false">取消</button>
      </div>
    </form>

    <section v-if="customGoals.length" class="planning-goal-list planning-goals-section">
        <article v-for="goal in customGoals" :key="goal.id" class="planning-goal" :class="[goal.status, { 'pace-behind': goal.can_finish === false, 'pace-on-track': goal.can_finish === true }]">
          <header>
            <span><b>{{ goal.note || `${goal.resource}目标` }}</b><small>{{ isJp ? (goal.goal_mode === 'amount_target' ? '数量目标 · 日期预测待适配' : `日期目标 · ${goalDeadline(goal)} 截止`) : goal.kind === 'fragment' ? '碎片目标 · 异去' : `${goal.goal_mode === 'stock_target' ? '活动目标' : goal.kind === 'event' ? '活动预算' : goal.goal_mode === 'amount_target' ? '数量目标' : goal.goal_mode === 'deadline_target' ? '日期目标' : '手动目标'} · ${goal.goal_mode === 'amount_target' ? `${goalDeadline(goal)} 预计达成` : `${goalDeadline(goal)} 截止`}` }}</small></span>
            <em>{{ goalStatusLabel(goal) }}</em>
            <button type="button" class="planning-delete" title="删掉这个目标" @click="removeGoal(goal.id)">×</button>
          </header>
          <template>
            <strong class="planning-goal-result">{{ goalHeadline(goal) }}</strong>
            <p v-if="goalAction(goal)" class="planning-next-action">{{ goalAction(goal) }}</p>
            <div class="planning-goal-progress">
              <progress v-if="goal.current != null && goal.target != null" :value="goalProgress(goal)" max="100" :aria-label="`${goal.resource}目标进度`" />
              <p><span>当前 <b>{{ fmt(goal.current) }}</b><template v-if="goal.target != null"> / {{ fmt(goal.target) }}</template> {{ goal.resource }}</span><span v-if="goalProgressMeta(goal)">{{ goalProgressMeta(goal) }}</span></p>
            </div>
            <ResourceGoalGuide
              v-if="goal.resource !== '小判' && (goal.kind || 'resource') === 'resource' && goal.goal_mode !== 'stock_target' && !['done', 'expired'].includes(goal.status) && acquisitionGuide(goal)"
              :guide="acquisitionGuide(goal)!"
            />
            <FragmentGoalGuide
              v-else-if="goal.kind === 'fragment'"
              :goal="goal"
              :guide="fragmentGuide(goal)"
              :notes="planning?.fragment_notes ?? null"
            />
          </template>
          <details>
            <summary>查看预测依据</summary>
            <p>{{ goal.message }}</p>
          </details>
        </article>
    </section>




    <GameplayPlanner v-if="!isJp" @goal-saved="gameplayGoalSaved" />
  </section>
</template>

<style scoped>
.planning-panel { display: grid; gap: 10px; }
.planning-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 38px; padding: 0 2px; }
.planning-toolbar > div { display: flex; align-items: baseline; gap: 8px; }
.planning-toolbar h3 { margin: 0; font-size: 18px; }
.planning-toolbar span { color: var(--ink-dim); font-size: 12px; }
.planning-error { margin: 0; padding: 10px 12px; color: #9f3d28; background: #f9e6df; border: 1px solid #d6a394; border-radius: 10px; }
.planning-success { margin: 0; padding: 9px 12px; color: #426b36; background: color-mix(in srgb, #dcebd6 72%, var(--paper-card)); border: 1px solid #a8c59c; border-radius: 10px; font-size: 13px; font-weight: 700; }
.planning-form { padding: 16px 18px; background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 12px; }
.planning-form > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.planning-form h4 { margin: 0; font-size: 16px; }
.planning-form header p { margin: 3px 0 0; color: var(--ink-dim); font-size: 13px; }
.planning-close { padding: 0 5px; color: var(--ink-dim); background: transparent; border: 0; font-size: 20px; cursor: pointer; }
.planning-form-fields { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
.planning-form label { display: grid; gap: 5px; color: var(--ink-dim); font-size: 12px; }
.planning-form input, .planning-form select { width: 100%; min-width: 0; }
.planning-form-actions { display: flex; gap: 8px; margin-top: 12px; }
.planning-goal-list { display: grid; gap: 10px; }
.planning-goals-section { scroll-margin-top: 12px; }
.planning-goal { padding: 16px 18px; background: var(--paper-card); border: 1px solid var(--paper-line); border-left: 5px solid var(--paper-line); border-radius: 12px; }
.planning-goal.done { border-left-color: #4d7a3a; }
.planning-goal.on_track { border-left-color: var(--fox-gold); }
.planning-goal.behind { border-left-color: #b0492e; }
.planning-goal.active { border-left-color: #5b813f; }
.planning-goal.pace-behind { border-left-color: #b0492e; }
.planning-goal.pace-on-track { border-left-color: #5b813f; }
.planning-goal.is-budget { border-left-color: #9b6652; }
.planning-goal > header { display: flex; align-items: flex-start; gap: 8px; }
.planning-goal > header > span { display: grid; gap: 1px; min-width: 0; }
.planning-goal > header small { color: var(--ink-dim); font-size: 11px; }
.planning-goal > header em { margin-left: auto; padding: 2px 9px; color: var(--ink-dim); border: 1px solid var(--paper-line); border-radius: 999px; font-size: 11px; font-style: normal; white-space: nowrap; }
.planning-goal.behind > header em { color: #9f3d28; border-color: #c98673; }
.planning-goal.on_track > header em { color: var(--fox-gold-deep); border-color: var(--fox-gold); }
.planning-goal.active > header em { color: #426b36; border-color: #77a164; }
.planning-goal.pace-behind > header em { color: #9f3d28; border-color: #c98673; }
.planning-goal.is-budget > header em { color: #784738; border-color: #b98775; }
.planning-delete { padding: 0 3px; color: var(--ink-dim); background: transparent; border: 0; font-size: 17px; cursor: pointer; }
.planning-goal-result { display: block; margin-top: 14px; font-size: clamp(22px, 3vw, 28px); line-height: 1.15; }
.planning-next-action { margin: 6px 0 0; color: var(--ink-dim); }
.planning-goal.behind .planning-next-action, .planning-goal.pace-behind .planning-next-action { color: #9f3d28; }
.planning-goal-progress { margin-top: 14px; }
.planning-goal-progress progress { display: block; width: 100%; height: 7px; overflow: hidden; accent-color: var(--fox-gold); border: 0; border-radius: 999px; }
.planning-goal.done .planning-goal-progress progress, .planning-goal.active .planning-goal-progress progress { accent-color: #5b813f; }
.planning-goal-progress p { display: flex; justify-content: space-between; gap: 12px; margin: 7px 0 0; color: var(--ink-dim); font-size: 11px; }
.planning-goal-progress b { color: var(--ink); }
.planning-panel details { margin-top: 10px; color: var(--ink-dim); font-size: 12px; }
.planning-panel summary { color: var(--fox-gold-deep); cursor: pointer; }
.planning-panel details p { margin: 7px 0 0; line-height: 1.6; }
.planning-empty-state { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 14px 16px; color: var(--ink-dim); background: var(--paper-card); border: 1px dashed var(--paper-line); border-radius: 10px; }
.planning-empty-state span { display: grid; gap: 2px; }
.planning-empty-state b { color: var(--ink); }
.planning-empty-state small { font-size: 11px; }
@media (max-width: 700px) {
  .planning-form-fields { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 520px) {
  .planning-form { padding: 14px; }
  .planning-form-fields { grid-template-columns: 1fr; }
  .planning-goal { padding: 14px; }
  .planning-goal-progress p { align-items: flex-start; flex-direction: column; gap: 3px; }
  .planning-empty-state { align-items: flex-start; flex-direction: column; }
}
</style>
