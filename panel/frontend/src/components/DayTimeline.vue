<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import GameplaySettingsDialog from './GameplaySettingsDialog.vue'
import { api } from '../api'
import type { ConductorBlockStatus, DayConductorBlock, DayExpeditionSuggestion, DayScheduleBlock, DayTimeline, DayTimelineExpedition, ScheduleBlockKind, ScriptParams, WorkflowPreset } from '../types'
import PaperCard from './PaperCard.vue'
import { draggedMinute } from './timelineDrag'
import { canAdoptRaidRecommendation, scheduleMinute } from './report/planningLinkModel'

const props = withDefaults(defineProps<{ collapsible?: boolean; refreshRequest?: number; adoptRecommendationRequest?: number }>(), {
  collapsible: false,
  adoptRecommendationRequest: 0,
})
const emit = defineEmits<{ openExpedition: []; timelineUpdated: [timeline: DayTimeline | null]; gameplaySettingsSaved: [script: string, params: ScriptParams] }>()

const DAY = 1680
// workflow/daily 没有可靠时长，预计收工按 30 分钟估算（与后端口径一致）
const GENERIC_BLOCK_MIN = 30
const MAX_BLOCKS = 6
const data = ref<DayTimeline | null>(null)
const expanded = ref(!props.collapsible)
const editing = ref(false)
const saving = ref(false)
const removing = ref(false)
const planMessage = ref('')
const expeditionMessage = ref('')
const togglingExpedition = ref('')
const adoptingSuggestion = ref('')
const prefsBusy = ref(false)
const conductorChoice = ref('')
const conductorBusy = ref(false)
const conductorMessage = ref('')
const workflowPresets = ref<WorkflowPreset[]>([])
interface DraftRow { time: string; kind: ScheduleBlockKind; runs: number; workflow_id: string; script: string; event_key: string }
const draft = ref<DraftRow[]>([])
const editedBlockIndex = ref<number | null>(null)
let editedBlockSnapshot = ''
const gameplayDialog = ref<InstanceType<typeof GameplaySettingsDialog>>()
const gameplayOptions = computed(() => data.value?.gameplay_options || [])
const firstGameplay = computed(() => gameplayOptions.value.find(option => option.available))
function gameplayLabel(script?: string) { return gameplayOptions.value.find(option => option.script === script)?.label || script || '玩法' }
function chooseGameplay(row: DraftRow) {
  row.kind = 'activity'
  row.event_key = gameplayOptions.value.find(option => option.script === row.script)?.event_key || ''
}
function gameplaySaved(script: string, params: ScriptParams) {
  emit('gameplaySettingsSaved', script, params)
  void load()
}
/** 运行图气泡点开「改参数」时要高亮的草稿行；-1 = 不高亮 */
const highlightIndex = ref(-1)
/** 运行图块气泡：块在 booking.blocks 里的下标 + 所在泳道 + 块左缘（%） */
const suggestedGameplay = ref<DayScheduleBlock | null>(null)
const planEditorDialog = ref<HTMLDialogElement>()
watch(editing, async value => { if (value) { await nextTick(); planEditorDialog.value?.showModal() } })
const popover = ref<{ index: number; lane: 'task' | 'daily'; left: number; origin?: 'booking' } | null>(null)
// 现在线走浏览器本地时钟平滑推进；30s 轮询只负责校准和数据
const smoothNowMin = ref(0)
let timer: number | undefined
let clockTimer: number | undefined
const refreshing = ref(false)
const refreshMessage = ref('')

async function refreshTimeline() {
  if (refreshing.value || drag.value || dragBusy.value) return
  refreshing.value = true
  refreshMessage.value = ''
  popover.value = null
  expeditionPopup.value = null
  try {
    refreshMessage.value = await load(true) ? '已刷新' : '刷新失败，请再试一次'
  } finally { refreshing.value = false }
}

async function load(force = false) {
  if (!force && (drag.value || dragBusy.value)) return
  try {
    const fresh = await api.dayTimeline()
    if (data.value && fresh.day_start !== data.value.day_start) suggestionDragTimes.value = {}
    fresh.suggestions?.forEach((s, i) => {
      const minute = suggestionDragTimes.value[`gameplay:${i}`]
      if (minute != null) s.start_min = minute
    })
    fresh.expedition_suggestions.forEach(s => {
      const minute = suggestionDragTimes.value[`expedition:${s.team_no}:${s.map_code}:${s.shift_no}`]
      if (minute != null) {
        movedExpeditionSources.value[s.key] = s.start_min
        s.start_min = minute
      }
    })
    data.value = fresh
    emit('timelineUpdated', data.value)
    syncSmoothClock()
    if (data.value.conductor.enabled) conductorChoice.value = data.value.conductor.workflow_id
    else if (!data.value.conductor.options.some(option => option.id === conductorChoice.value)) {
      conductorChoice.value = data.value.conductor.workflow_id
    }
    return true
  } catch {
    /* 静默失败，下轮轮询再试 */
    return false
  }
}

function syncSmoothClock() {
  if (!data.value) return
  smoothNowMin.value = Math.min(DAY, Math.max(0, (Date.now() / 1000 - data.value.day_start) / 60))
}

async function loadWorkflowPresets() {
  try {
    workflowPresets.value = (await api.workflows()).presets
  } catch {
    /* 下拉暂时为空，保存时后端还会再核 */
  }
}

async function resumeRaid(block: DayConductorBlock, finishedRound: boolean) {
  if (conductorBusy.value || !block.run_id) return
  conductorBusy.value = true
  conductorMessage.value = ''
  try {
    await api.resumeDayRaid(block.run_id, finishedRound)
    await load()
    conductorMessage.value = '剩下的联队战继续开工了。'
  } catch (error) {
    conductorMessage.value = error instanceof Error ? error.message : '暂时没能继续'
  } finally {
    conductorBusy.value = false
  }
}

async function stopConductor() {
  if (conductorBusy.value) return
  conductorBusy.value = true
  conductorMessage.value = ''
  try {
    await api.setDayConductor(false)
    await load()
    conductorMessage.value = '后续时段不会自动开工；已经开工的这一段仍需在执务台停止。'
  } catch (error) {
    conductorMessage.value = error instanceof Error ? error.message : '大总管没改成，请重试'
    await load()
  } finally {
    conductorBusy.value = false
  }
}

async function toggleExpedition(slot: DayTimelineExpedition) {
  if (!slot.toggleable || togglingExpedition.value) return
  togglingExpedition.value = slot.key
  expeditionMessage.value = ''
  try {
    await api.setDayExpeditionSlot(slot.key, !slot.will_run)
    await load()
    expeditionMessage.value = !slot.will_run
      ? '这班已点上，到点单独派出。'
      : '这班今天不跑了；想跑再点建议淡影。'
  } catch (error) {
    expeditionMessage.value = error instanceof Error ? error.message : '这班没改成，请重试'
    await load()
  } finally {
    togglingExpedition.value = ''
  }
}

/* ── 远征建议：偏好即存即生效；点淡影 = 采纳（那班记 forced，不会自动跑） ── */

async function updateRounds(event: Event) {
  const value = Number((event.target as HTMLSelectElement).value)
  if (!data.value || prefsBusy.value || adoptingSuggestion.value) return
  const previous = data.value.expedition_help.rounds_per_team
  data.value.expedition_help.rounds_per_team = value
  prefsBusy.value = true
  expeditionMessage.value = ''
  try {
    await api.setExpeditionHelpPrefs(value, data.value.expedition_help.available_teams)
    await load()
  } catch (error) {
    expeditionMessage.value = error instanceof Error ? error.message : '偏好没存上，请重试'
    if (data.value) data.value.expedition_help.rounds_per_team = previous
    await load()
  } finally {
    prefsBusy.value = false
  }
}

async function toggleAvailableTeam(team: number) {
  if (!data.value || prefsBusy.value || adoptingSuggestion.value) return
  const current = data.value.expedition_help.available_teams
  const next = current.includes(team)
    ? current.filter(item => item !== team)
    : [...current, team].sort((a, b) => a - b)
  const previous = current
  data.value.expedition_help.available_teams = next
  prefsBusy.value = true
  expeditionMessage.value = ''
  try {
    await api.setExpeditionHelpPrefs(data.value.expedition_help.rounds_per_team, next)
    await load()
  } catch (error) {
    expeditionMessage.value = error instanceof Error ? error.message : '偏好没存上，请重试'
    if (data.value) data.value.expedition_help.available_teams = previous
    await load()
  } finally {
    prefsBusy.value = false
  }
}

const expeditionPopup = ref<{team: number; left: number; suggestion?: DayExpeditionSuggestion; slot?: DayTimelineExpedition} | null>(null)
const expeditionDialog = ref<HTMLDialogElement>()
const expeditionTime = ref('')
const expeditionFormation = ref('')
const expeditionSakura = ref(false)
const expeditionInjury = ref<'light' | 'medium' | 'heavy'>('light')
const expeditionConfigError = ref('')
const expeditionConfigBusy = ref(false)
const expeditionSettings = ref<Awaited<ReturnType<typeof api.expeditionSettings>>>()
const configuringExpedition = ref<typeof expeditionPopup.value>(null)
const expeditionRewards = computed(() => Object.entries(expeditionSettings.value?.rewards || {}).map(([name, count]) => `${count.toLocaleString()} ${name}`).join('、'))
const expeditionPresetOptions = computed(() => expeditionSettings.value?.formations[String(configuringExpedition.value?.team)] || [])
function openExpeditionPopup(team: number, left: number, suggestion?: DayExpeditionSuggestion, slot?: DayTimelineExpedition) {
  popover.value = null
  expeditionPopup.value = {team, left, suggestion, slot}
}
async function configureExpedition() {
  const selected = expeditionPopup.value
  if (!selected) return
  configuringExpedition.value = selected
  expeditionPopup.value = null
  expeditionTime.value = fmtMin(selected.suggestion?.start_min ?? selected.slot!.time_min).replace('次日 ', '')
  expeditionFormation.value = selected.suggestion?.formation_id || selected.slot?.formation_id || ''
  expeditionSakura.value = selected.slot?.sakura_before_dispatch ?? false
  expeditionInjury.value = selected.slot?.repair_threshold ?? 'light'
  expeditionSettings.value = undefined
  expeditionConfigError.value = ''
  expeditionDialog.value?.showModal()
  expeditionConfigBusy.value = true
  try { expeditionSettings.value = await api.expeditionSettings(selected.suggestion?.map_code || selected.slot!.map_code) }
  catch (error) { expeditionConfigError.value = error instanceof Error ? error.message : '读取配置失败' }
  finally { expeditionConfigBusy.value = false }
}
async function saveExpeditionConfiguration() {
  const selected = configuringExpedition.value
  const start = parseTime(expeditionTime.value)
  if (!selected || start == null || expeditionConfigBusy.value || !expeditionSettings.value) return
  expeditionConfigBusy.value = true
  expeditionConfigError.value = ''
  try {
    const preset = expeditionPresetOptions.value.find(p => p.formation_id === expeditionFormation.value)
    if (expeditionFormation.value && !preset) throw new Error('原预设已不可用，请重新选择')
    await api.configureDayExpedition({team_no: selected.team, map_code: selected.suggestion?.map_code || selected.slot!.map_code,
      start_min: start, ...(selected.slot ? {source_key: selected.slot.key} : {source_start_min: movedExpeditionSources.value[selected.suggestion!.key] ?? selected.suggestion!.start_min}),
      sakura_before_dispatch: expeditionSakura.value, repair_threshold: expeditionInjury.value,
      formation_id: preset?.formation_id || '', formation_signature: preset?.formation_signature || ''})
    expeditionDialog.value?.close()
    await load()
  } catch (error) { expeditionConfigError.value = error instanceof Error ? error.message : '这班没有保存，请重试' }
  finally { expeditionConfigBusy.value = false }
}
async function removeExpeditionFromPopup() {
  const selected = expeditionPopup.value
  if (!selected) return
  if (selected.suggestion) {
    if (adoptingSuggestion.value) return
    const suggestion = selected.suggestion
    adoptingSuggestion.value = suggestion.key
    expeditionMessage.value = ''
    try {
      if (movedExpeditionSources.value[suggestion.key] != null) {
        await api.configureDayExpedition({team_no: selected.team, map_code: suggestion.map_code,
          start_min: suggestion.start_min, source_start_min: movedExpeditionSources.value[suggestion.key],
          formation_id: suggestion.formation_id || '', formation_signature: suggestion.formation_signature || ''})
      } else await api.adoptDayExpeditionSuggestion(selected.team, suggestion.map_code, suggestion.start_min, suggestion.formation_id, suggestion.formation_signature)
      expeditionPopup.value = null
      await load()
    } catch (error) {
      expeditionMessage.value = error instanceof Error ? error.message : '这班没有启用，请重试'
    } finally { adoptingSuggestion.value = '' }
  } else if (selected.slot) {
    expeditionPopup.value = null
    await toggleExpedition(selected.slot)
  }
}

function closePopoverOnOutside(event: MouseEvent) {
  const target = event.target as HTMLElement | null
  if (!target) return
  if (target.closest('.tl-popover') || target.closest('.tl-block') || target.closest('.tl-booking-link')) return
  expeditionPopup.value = null
  popover.value = null
}

function closePopoverOnEscape(event: KeyboardEvent) {
  if (event.key === 'Escape') { popover.value = null; expeditionPopup.value = null }
}

onMounted(() => {
  load()
  syncSmoothClock()
  void loadWorkflowPresets()
  timer = window.setInterval(load, 30000)
  clockTimer = window.setInterval(() => {
    smoothNowMin.value = Math.min(DAY, smoothNowMin.value + 1 / 60)
  }, 1000)
  document.addEventListener('click', closePopoverOnOutside)
  document.addEventListener('keydown', closePopoverOnEscape)
})
onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
  if (clockTimer) window.clearInterval(clockTimer)
  document.removeEventListener('click', closePopoverOnOutside)
  document.removeEventListener('keydown', closePopoverOnEscape)
})

const TICKS = [0, 240, 480, 720, 960, 1200, 1440, 1680]
const MINI_TICKS = [0, 480, 960, 1440, 1680]
const COMPACT_LIMIT = 4

const TEAM_NAMES: Record<number, string> = { 1: '一', 2: '二', 3: '三', 4: '四', 5: '五' }

const STATE_LABELS: Record<string, string> = {
  pending: '待派出',
  skipped: '今天跳过',
  waiting_busy: '等空位',
  waiting_unknown: '等确认',
  ready: '可派出',
  dispatched: '已派出',
  expired: '已跳过',
  missed: '已错过',
  failed_unknown: '派遣失败',
  running: '远征中',
  awaiting_collect: '待收菜',
}

const STATE_CLASSES: Record<string, string> = {
  pending: 'is-pending',
  skipped: 'is-skipped',
  waiting_busy: 'is-waiting',
  waiting_unknown: 'is-waiting',
  ready: 'is-ready',
  dispatched: 'is-done',
  expired: 'is-expired',
  missed: 'is-missed',
  failed_unknown: 'is-failed',
  running: 'is-running',
  awaiting_collect: 'is-waiting',
}

const TONE_LABELS: Record<string, string> = {
  waiting: '等待继续',
  ok: '顺利完成',
  failed: '翻车',
  stopped: '被叫停',
  running: '正在跑',
}

function pct(min: number): number {
  return Math.min(100, Math.max(0, (min / DAY) * 100))
}

function fmtClock(min: number): string {
  const h = Math.floor(min / 60) % 24
  const m = Math.floor(min % 60)
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

function fmtMin(min: number): string {
  return `${min >= 1440 ? '次日 ' : ''}${fmtClock(min)}`
}

function fmtTs(ts: number): string {
  const d = new Date(ts * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function tickLabel(min: number): string {
  return min >= 1440 ? `次日 ${min / 60 - 24}:00` : `${min / 60}:00`
}

function durationText(min: number): string {
  if (min <= 0) return ''
  if (min < 60) return `${min}分`
  const hours = Math.floor(min / 60)
  const minutes = min % 60
  return minutes ? `${hours}小时${minutes}分` : `${hours}小时`
}

function parseTime(value: string): number | null {
  return scheduleMinute(value)
}

const raidKindAvailable = computed(() => data.value?.activity?.name === '联队战')

const nowMin = computed(() => {
  if (!data.value) return 0
  return Math.min(DAY, Math.max(0, (data.value.now - data.value.day_start) / 60))
})

function presetName(id?: string): string {
  if (!id) return ''
  return workflowPresets.value.find(preset => preset.id === id)?.name || `任务流 ${id}`
}

function blockLabel(block: DayScheduleBlock): string {
  if (block.kind === 'raid') return `联队战 ${block.runs ?? '?'} 圈`
  if (block.kind === 'activity') return `${gameplayLabel(block.script)} ${block.runs ?? '?'} 次`
  if (block.kind === 'workflow') return presetName(block.workflow_id) || '任务流'
  return '一键日课'
}

/** 预计收工时刻；raid 没有活动卡片时算不出来，返回 null */
function blockEndMin(block: DayScheduleBlock): number | null {
  const activity = data.value?.activity
  if (block.kind === 'raid') {
    if (!activity || activity.name !== '联队战') return null
    return block.start_min + Math.ceil((block.runs ?? 1) * activity.seconds_per_loop / 60)
  }
  if (block.kind === 'activity' || block.kind === 'workflow') return null
  return block.start_min + GENERIC_BLOCK_MIN
}

function conductorBlockFor(block: DayScheduleBlock): DayConductorBlock | undefined {
  if (!data.value?.conductor.enabled) return undefined
  return data.value.conductor.blocks.find(
    candidate => candidate.start_min === block.start_min && candidate.kind === block.kind && candidate.script === block.script)
}

const STATUS_TEXT: Partial<Record<Exclude<ConductorBlockStatus, 'pending'>, string>> = {
  running: '正在跑',
  ended: '已收工',
  missed: '换日未跑',
}

function blockStatusText(block: DayConductorBlock): string {
  if (block.waiting_until) return `等待至 ${clockAt(block.waiting_until)}，其他任务可开工`
  if (block.status === 'pending') {
    // 过点不是错误：runner 忙完手头的活就排队开工
    return block.start_min <= smoothNowMin.value ? '排队中，手头收工就上' : '到点开工'
  }
  if (block.status === 'blocked') return `没开工：${block.reason || '核对没过'}`
  if (block.status === 'interrupted') return block.reason || '中断'
  return STATUS_TEXT[block.status] || block.status
}

const STATUS_CLASSES: Partial<Record<Exclude<ConductorBlockStatus, 'pending'>, string>> = {
  running: 'is-running',
  ended: 'is-done',
  blocked: 'is-failed',
  interrupted: 'is-failed',
  missed: 'is-expired',
}

function blockStatusClass(block: DayConductorBlock): string {
  if (block.status === 'pending') {
    return block.start_min <= smoothNowMin.value ? 'is-waiting' : 'is-pending'
  }
  return STATUS_CLASSES[block.status] || 'is-pending'
}

function toDraftRow(block: DayScheduleBlock): DraftRow {
  return { time: fmtClock(block.start_min), kind: block.kind, runs: block.runs ?? 1, workflow_id: block.workflow_id || '', script: block.script || (block.kind === 'raid' ? 'raid' : ''), event_key: block.event_key || '' }
}

function recommendedBlocks(): DayScheduleBlock[] {
  return (data.value?.suggestions || [])
    .filter(block => block.runs != null)
    .map(block => ({ start_min: block.start_min, kind: 'raid' as const, runs: block.runs! }))
}

function defaultDraftRow(): DraftRow {
  return {
    time: fmtClock(Math.min(DAY - 1, Math.ceil(nowMin.value))),
    kind: firstGameplay.value ? 'activity' : 'workflow',
    script: firstGameplay.value?.script || '',
    event_key: firstGameplay.value?.event_key || '',
    runs: 1,
    workflow_id: '',
  }
}

function editPlan() {
  const block = data.value?.booking?.blocks[highlightIndex.value]
  if (!block) return
  editedBlockIndex.value = highlightIndex.value
  editedBlockSnapshot = JSON.stringify(block)
  draft.value = [toDraftRow(block)]
  planMessage.value = ''
  void loadWorkflowPresets()
  editing.value = true
}

async function addTimedWorkflow() {
  await loadWorkflowPresets()
  editedBlockIndex.value = -1
  draft.value = [{...defaultDraftRow(), kind: 'workflow', workflow_id: workflowPresets.value[0]?.id || ''}]
  planMessage.value = ''
  editing.value = true
}

function clockAt(at: number): string {
  return new Date(at * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
}

function rowStart(row: DraftRow): number | null {
  return parseTime(row.time)
}

function presetSteps(id: string) {
  return workflowPresets.value.find(preset => preset.id === id)?.steps || []
}

async function cancelWorkflowWait(id: string) {
  try {
    await api.cancelWorkflowWait(id)
    planMessage.value = '已取消这份任务流的后续步骤。'
    await load()
  } catch (e) {
    planMessage.value = (e as Error).message
  }
}

/** 按推荐安排 = 把建议块填进草稿等确认，不直接保存 */
function fillRecommended() {
  editedBlockIndex.value = null
  highlightIndex.value = -1
  const blocks = recommendedBlocks()
  draft.value = blocks.length ? blocks.map(toDraftRow) : [defaultDraftRow()]
  planMessage.value = ''
  void loadWorkflowPresets()
  editing.value = true
}

/** Cross-card adoption only opens an untouched recommendation draft; it never saves or starts work. */
async function openRaidRecommendation() {
  const blocks = recommendedBlocks()
  const canAdopt = canAdoptRaidRecommendation(
    blocks.length > 0,
    editing.value,
    (data.value?.booking?.blocks.length || 0) > 0,
  )
  if (!blocks.length) return false
  expanded.value = true
  if (!canAdopt) {
    planMessage.value = editing.value
      ? '你正在修改时间表草稿，先保存或取消这份草稿，再带入联队战建议。'
      : '时间表里已经有安排，先在这里处理现有安排，再带入联队战建议。'
    await nextTick()
    const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
    document.querySelector('.tl-booking')?.scrollIntoView({ behavior, block: 'start' })
    return false
  }
  fillRecommended()
  await nextTick()
  const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
  document.querySelector('.tl-booking-editor')?.scrollIntoView({ behavior, block: 'start' })
  return true
}

watch(() => props.refreshRequest, () => { void load() })

watch(() => props.adoptRecommendationRequest, (request, previous) => {
  if (request > previous) void openRaidRecommendation()
})

/** 点建议淡影先打开操作菜单，保存配置后才采纳。 */
function adoptSuggestion(shadow: { minute: number; runs?: number; left: number }) {
  expeditionPopup.value = null
  suggestedGameplay.value = {start_min: shadow.minute, kind: 'raid', runs: shadow.runs ?? 1}
  popover.value = {index: -1, lane: 'task', left: shadow.left}
}

function openGameplayConfiguration(block: DayScheduleBlock, index = -1, chooseGameplay = false) {
  const script = block.script || 'raid'
  const original = index >= 0 ? JSON.stringify(data.value?.booking?.blocks[index]) : ''
  popover.value = null
  void gameplayDialog.value?.open(script, {time: fmtClock(block.start_min), runs: block.runs ?? 1,
    choices: chooseGameplay ? gameplayOptions.value : undefined,
    choose: (key, time, runs) => {
      const option = gameplayOptions.value.find(item => item.script === key && item.available)
      if (!option) return
      openGameplayConfiguration({kind: key === 'raid' ? 'raid' : 'activity', script: key, event_key: option.event_key, start_min: parseTime(time) ?? block.start_min, runs}, -1, true)
    },
    save: async (params, time, runs) => {
      const start = parseTime(time)
      if (start == null || start < Math.floor(smoothNowMin.value)) throw new Error('请选择现在至次日03:59之间的开始时间')
      const blocks = [...(data.value?.booking?.blocks || [])]
      if (index >= 0 && JSON.stringify(blocks[index]) !== original) throw new Error('这段安排已经变化，请关闭后重新配置')
      const cblock = index >= 0 && blocks[index] ? conductorBlockFor(blocks[index]) : undefined
      if (cblock && cblock.status !== 'pending') throw new Error('这段已经执行或停止，请另行安排')
      const updated = { ...block, start_min: start, runs }
      if (index >= 0) blocks[index] = updated
      else blocks.push(updated)
      if (blocks.length > MAX_BLOCKS) throw new Error('今天最多安排6段')
      const result = await api.saveGameplaySettings(script, params)
      await persistSchedule(blocks.sort((a, b) => a.start_min - b.start_min), '')
      return result.params
    }})
}
function addGameplayConfiguration() {
  const option = firstGameplay.value
  if (!option) return
  const block: DayScheduleBlock = option.script === 'raid'
    ? {kind: 'raid', start_min: Math.ceil(smoothNowMin.value), runs: 1}
    : {kind: 'activity', script: option.script, event_key: option.event_key, start_min: Math.ceil(smoothNowMin.value), runs: 1}
  openGameplayConfiguration(block, -1, true)
}
function configureBookedBlock(block: DayScheduleBlock, index: number) {
  if (block.kind === 'raid' || block.kind === 'activity') openGameplayConfiguration(block, index)
  else { highlightIndex.value = index; editPlan() }
}

const draftHasRaid = computed(() => draft.value.some(row => row.kind === 'raid'))

const preview = computed(() => {
  const activity = data.value?.activity
  const issues: string[] = []
  const blocks: DayScheduleBlock[] = []
  const hasRaid = draftHasRaid.value
  if (hasRaid && !raidKindAvailable.value) {
    issues.push('当前没有可安排的联队战，联队战段请移除或换成别的活')
  }
  const pace = activity?.name === '联队战' ? activity.seconds_per_loop : 0
  if (hasRaid && pace <= 0) issues.push('还没有可靠的本期圈速')
  const deadline = activity
    ? Math.min(DAY, Math.floor((activity.event_end_at - (data.value?.day_start || 0)) / 60) - 5)
    : 0
  const loopMin = pace > 0 ? pace : 420
  let totalRuns = 0
  const spans: Array<[number, number]> = []
  for (const [index, row] of draft.value.entries()) {
    const start = rowStart(row)
    if (start == null) {
      issues.push(`第${index + 1}段请填写今天的时间`)
      continue
    }
    if (row.kind === 'raid') {
      const runs = Number(row.runs)
      if (!Number.isInteger(runs) || runs < 1 || runs > 99) {
        issues.push(`第${index + 1}段圈数要填 1–99`)
        continue
      }
      if (!raidKindAvailable.value) continue
      blocks.push({ start_min: start, kind: 'raid', runs })
      totalRuns += runs
      const end = start + Math.ceil(runs * loopMin / 60)
      spans.push([start, end])
      if (end > deadline) issues.push(`第${index + 1}段赶不上今天收摊`)
      const collision = activity?.occupied.find(item => start < item.end_min && end > item.start_min)
      if (collision) issues.push(`第${index + 1}段会撞上${collision.label}`)
    } else if (row.kind === 'activity') {
      const option = gameplayOptions.value.find(item => item.script === row.script)
      if (!option?.available || option.event_key !== row.event_key) {
        issues.push(`第${index + 1}段玩法未开放或已换期，请重新选择`)
        continue
      }
      if (!Number.isInteger(row.runs) || row.runs < 1 || row.runs > 99) {
        issues.push(`第${index + 1}段次数要填 1–99`)
        continue
      }
      if (option.end_at && Math.max((data.value?.day_start || 0) + start * 60, data.value?.now || 0) >= option.end_at) {
        issues.push(`第${index + 1}段开工时活动已结束`)
      }
      blocks.push({ start_min: start, kind: 'activity', script: row.script, event_key: row.event_key, runs: row.runs })
      spans.push([start, start + GENERIC_BLOCK_MIN])
    } else if (row.kind === 'workflow') {
      if (!row.workflow_id) {
        issues.push(`第${index + 1}段选一个任务流`)
        continue
      }
      blocks.push({ start_min: start, kind: 'workflow', workflow_id: row.workflow_id })
      spans.push([start, start + 1])
    } else {
      blocks.push({ start_min: start, kind: 'daily' })
      spans.push([start, start + GENERIC_BLOCK_MIN])
    }
  }
  if (hasRaid && raidKindAvailable.value && totalRuns > (activity?.remaining_runs ?? 0)) {
    issues.push('圈数超过本期剩余圈数')
  }
  for (let i = 1; i < spans.length; i++) {
    if (spans[i][0] < spans[i - 1][0]) issues.push('请按开工时间排列时段')
    if (spans[i - 1][1] > spans[i][0]) {
      issues.push('时段互相重叠')
      break
    }
  }
  return { blocks, issues: [...new Set(issues)] }
})

function rowEndText(row: DraftRow): string {
  const start = rowStart(row)
  if (start == null) return ''
  if (row.kind === 'raid') {
    const activity = data.value?.activity
    if (!activity || activity.name !== '联队战') return ''
    return fmtMin(start + Math.ceil(Number(row.runs || 0) * activity.seconds_per_loop / 60))
  }
  if (row.kind === 'activity' || row.kind === 'workflow') return ''
  return fmtMin(start + GENERIC_BLOCK_MIN)
}

async function persistSchedule(blocks: DayScheduleBlock[], message: string) {
  const result = await api.saveDaySchedule(
    blocks, blocks.some(block => block.kind === 'raid') ? 'builtin-scheduled-raid' : undefined)
  if (data.value) {
    data.value.conductor = result.conductor
    data.value.booking = result.booking
    emit('timelineUpdated', data.value)
  }
  planMessage.value = message
}

async function saveSchedule() {
  saving.value = true
  planMessage.value = ''
  try {
    const blocks = editedBlockIndex.value == null ? [...preview.value.blocks] : [...(data.value?.booking?.blocks || [])]
    if (editedBlockIndex.value != null) {
      const index = editedBlockIndex.value
      if (index >= 0 && JSON.stringify(blocks[index]) !== editedBlockSnapshot) throw new Error('这段安排已经变化，请关闭后重新配置')
      if (index >= 0) blocks.splice(index, 1, ...preview.value.blocks)
      else blocks.push(...preview.value.blocks)
    }
    blocks.sort((a, b) => a.start_min - b.start_min)
    await persistSchedule(blocks, '')
    editing.value = false
    highlightIndex.value = -1
  } catch (error) {
    planMessage.value = error instanceof Error ? error.message : '保存失败，请重试'
    await load()
  } finally {
    saving.value = false
  }
}

const popoverBlock = computed(() => {
  if (popover.value == null) return undefined
  return popover.value.index === -1 ? suggestedGameplay.value || undefined : data.value?.booking?.blocks[popover.value.index]
})

const overlappingTasks = ref<Array<{ index: number; block: DayScheduleBlock }>>([])
const drag = ref<{ key: string; start: number; minute: number; x: number; width: number; moved: boolean; kind: 'task' | 'suggestion' | 'expedition' | 'expeditionSuggestion'; index: number } | null>(null)
const movedExpeditionSources = ref<Record<string, number>>({})
const suggestionDragTimes = ref<Record<string, number>>({})
let suppressClick = false
const dragBusy = ref(false)
function dragLeft(key: string, original: number) { return drag.value?.key === key ? pct(drag.value.minute) : original }
function startDrag(event: PointerEvent, key: string, kind: 'task' | 'suggestion' | 'expedition' | 'expeditionSuggestion', start: number, index = -1) {
  if (event.button !== 0 || dragBusy.value || editing.value) return
  if (kind === 'task') {
    const block = data.value?.booking?.blocks[index]
    const receipt = data.value?.conductor.blocks.find(b => b.start_min === block?.start_min && b.kind === block?.kind && b.script === block?.script)
    if (!block || (receipt?.status && receipt.status !== 'pending')) return
  }
  if (kind === 'expedition') {
    const slot = data.value?.expeditions.find(s => s.key === key)
    if (!slot?.toggleable || !key.includes(':adhoc:') || slot.state !== 'pending') return
  }
  const button = event.currentTarget as HTMLButtonElement
  drag.value = {key, kind, start, minute: start, index, x: event.clientX,
    width: button.closest('.tl-lane')!.getBoundingClientRect().width, moved: false}
  button.setPointerCapture(event.pointerId)
}
function moveDrag(event: PointerEvent) {
  const current = drag.value
  if (!current) return
  if (Math.abs(event.clientX - current.x) < 5 && !current.moved) return
  current.moved = true
  current.minute = draggedMinute(current.start, event.clientX - current.x, current.width, smoothNowMin.value)
}
async function finishDrag(event: PointerEvent) {
  const current = drag.value
  if (!current) return
  drag.value = null
  if (!current.moved) return
  suppressClick = true
  window.setTimeout(() => { suppressClick = false }, 0)
  if (event.type === 'pointercancel' || current.minute === current.start || !data.value) return
  popover.value = null
  expeditionPopup.value = null
  dragBusy.value = true
  planMessage.value = ''
  try {
    if (current.kind === 'task') {
      const blocks = data.value.booking!.blocks.map((b, i) => i === current.index ? {...b, start_min: current.minute} : {...b})
      const moved = blocks[current.index]!
      const duration = (blockEndMin(moved) ?? moved.start_min + GENERIC_BLOCK_MIN) - moved.start_min
      if (blocks.some((b, i) => i !== current.index && current.minute < (blockEndMin(b) ?? b.start_min + GENERIC_BLOCK_MIN) && current.minute + duration > b.start_min)) throw new Error('这个时间会撞上已有任务，请换个位置')
      await persistSchedule(blocks.sort((a, b) => a.start_min - b.start_min), `已改到 ${fmtMin(current.minute)} 开工`)
    } else if (current.kind === 'suggestion') {
      const suggestion = data.value.suggestions?.[current.index]
      if (suggestion && (data.value.booking?.blocks || []).some(b => current.minute < (blockEndMin(b) ?? b.start_min + GENERIC_BLOCK_MIN) && current.minute + suggestion.duration_min > b.start_min)) throw new Error('这个时间会撞上已有任务，请换个位置')
      if (suggestion) suggestion.start_min = current.minute
      suggestionDragTimes.value[`gameplay:${current.index}`] = current.minute
      planMessage.value = ''
    } else if (current.kind === 'expeditionSuggestion') {
      const suggestion = data.value.expedition_suggestions.find(s => s.key === current.key)
      if (suggestion) {
        movedExpeditionSources.value[current.key] ??= suggestion.start_min
        suggestion.start_min = current.minute
        suggestionDragTimes.value[`expedition:${suggestion.team_no}:${suggestion.map_code}:${suggestion.shift_no}`] = current.minute
      }
      planMessage.value = ''
    } else {
      const slot = data.value.expeditions.find(s => s.key === current.key)!
      const settings = await api.expeditionSettings(slot.map_code)
      const preset = (settings.formations[String(slot.team_no)] || []).find(p => p.formation_id === slot.formation_id)
      if (slot.formation_id && !preset) throw new Error('原预设已不可用，请先打开配置')
      await api.configureDayExpedition({team_no: slot.team_no, map_code: slot.map_code,
        source_key: slot.key, start_min: current.minute, formation_id: preset?.formation_id || '', formation_signature: preset?.formation_signature || '',
        sakura_before_dispatch: slot.sakura_before_dispatch ?? false, repair_threshold: slot.repair_threshold ?? 'light'})
      await load(true)
      planMessage.value = `远征已改到 ${fmtMin(current.minute)} 出发`
    }
  } catch (error) { planMessage.value = error instanceof Error ? error.message : '没有保存，请重试' }
  finally { dragBusy.value = false }
}
const dragEnd = computed(() => {
  const current = drag.value
  if (!current || !data.value) return null
  let duration = GENERIC_BLOCK_MIN
  if (current.kind === 'task') {
    const block = data.value.booking?.blocks[current.index]
    if (block) duration = (blockEndMin(block) ?? block.start_min + GENERIC_BLOCK_MIN) - block.start_min
  } else if (current.kind === 'suggestion') duration = data.value.suggestions?.[current.index]?.duration_min ?? duration
  else if (current.kind === 'expedition') duration = data.value.expeditions.find(s => s.key === current.key)?.duration_min ?? duration
  else duration = data.value.expedition_suggestions.find(s => s.key === current.key)?.duration_min ?? duration
  return current.minute + duration
})
function openLaneTask(event: MouseEvent, index: number, shadow?: typeof suggestionShadows.value[number]) {
  if (suppressClick) return
  if (shadow) adoptSuggestion(shadow)
  else {
    const entry = taskBlocks.value.find(b => b.index === index)
    if (entry) openBlockPopover(entry)
  }
  const lane = (event.currentTarget as HTMLElement).closest('.tl-lane')!
  overlappingTasks.value = [...lane.querySelectorAll<HTMLButtonElement>('button[data-task-index]')]
    .filter(button => {
      const rect = button.getBoundingClientRect()
      return event.clientX >= rect.left && event.clientX <= rect.right
    }).flatMap(button => {
      const i = Number(button.dataset.taskIndex)
      if (i >= 0) {
        const block = data.value?.booking?.blocks[i]
        return block ? [{index: i, block}] : []
      }
      const suggestion = suggestionShadows.value.find(s => s.key === button.dataset.suggestionKey)
      return suggestion ? [{index: -1, block: {start_min: suggestion.minute, kind: 'raid' as const, runs: suggestion.runs ?? 1}}] : []
    })
}
function selectPopoverTask(entry: { index: number; block: DayScheduleBlock }) {
  if (!popover.value) return
  popover.value.index = entry.index
  suggestedGameplay.value = entry.index === -1 ? entry.block : null
}
function configureLaneTask(entry: { index: number; block: DayScheduleBlock }) {
  selectPopoverTask(entry)
  editFromPopover()
}
function actOnLaneTask(entry: { index: number; block: DayScheduleBlock }) {
  selectPopoverTask(entry)
  void removeFromSchedule()
}

function openBlockPopover(entry: { index: number; lane: 'task' | 'daily'; left: number; origin?: 'booking' }) {
  overlappingTasks.value = []
  expeditionPopup.value = null
  suggestedGameplay.value = null
  popover.value = { index: entry.index, lane: entry.lane, left: entry.left, origin: entry.origin }
}

function popoverLeft(left: number): string {
  return `max(0px, min(${left}%, calc(100% - 240px)))`
}

/** 气泡「配置」：玩法单独配置，任务流打开安排编辑器。 */
function editFromPopover() {
  if (popover.value == null) return
  const index = popover.value.index
  const block = popoverBlock.value
  if (!block) return
  configureBookedBlock(block, index)
  popover.value = null
}

/** 气泡「移出安排」：删掉这块走同一个保存即开工端点重存（保持 armed） */
async function removeFromSchedule() {
  if (popover.value?.index === -1 && suggestedGameplay.value) {
    if (removing.value) return
    removing.value = true
    planMessage.value = ''
    try {
      const blocks = [...(data.value?.booking?.blocks || []), {...suggestedGameplay.value}]
      await persistSchedule(blocks.sort((a, b) => a.start_min - b.start_min), '')
      popover.value = null
      suggestedGameplay.value = null
    } catch (error) {
      planMessage.value = error instanceof Error ? error.message : '这段没有启用，请重试'
    } finally { removing.value = false }
    return
  }
  const booking = data.value?.booking
  if (!booking || popover.value == null || removing.value) return
  const remaining = booking.blocks.filter((_, index) => index !== popover.value!.index)
  removing.value = true
  planMessage.value = ''
  try {
    await persistSchedule([...remaining].sort((a, b) => a.start_min - b.start_min),
      remaining.length ? '这一段已移出，剩下的照常到点开工。' : '安排已移除，不再定时启动。')
  } catch (error) {
    planMessage.value = error instanceof Error ? error.message : '移出失败，请重试'
    await load()
  } finally {
    removing.value = false
    popover.value = null
  }
}

const expeditionBlocks = computed(() => {
  if (!data.value) return []
  return data.value.expeditions.map((e) => {
    const team = TEAM_NAMES[e.team_no] ?? String(e.team_no)
    const stateLabel = STATE_LABELS[e.state] ?? e.state
    const visibleDuration = Math.min(Math.max(e.duration_min, 10), DAY - e.time_min)
    const bits = [`${fmtMin(e.time_min)} 部队${team} ${e.map_code}`]
    if (e.duration_min) bits[0] += `（${durationText(e.duration_min)}）`
    bits.push(stateLabel)
    if (e.formation_name) bits.push(`先换「${e.formation_name}」再派出`)
    if (e.late_min && e.state === 'dispatched') bits.push(`晚${e.late_min}分钟`)
    if (e.blocked_reason) bits.push(e.blocked_reason)
    if (e.kind === 'forced' && e.will_run) bits.push('点的班，到点单独派出')
    return {
      key: e.key,
      slot: e,
      minute: e.time_min,
      left: pct(e.time_min),
      width: Math.max(pct(visibleDuration), 0.7),
      cls: e.will_run ? 'tlx-on' : 'tlx-off',
      title: `${bits.join(' · ')}${e.toggleable ? ' · 点击配置或移除' : ''}`,
      text: e.map_code,
      rowTitle: `部队${team} · ${e.map_code}`,
      rowDetail: e.state === 'failed_unknown' ? `派遣失败${e.blocked_reason ? `：${e.blocked_reason}` : ''}` : `${durationText(e.duration_min)}远征 · ${stateLabel}`,
      time: fmtMin(e.time_min),
      tone: STATE_CLASSES[e.state] ?? 'is-pending',
      enabled: e.enabled,
    }
  })
})

// v2：上轴的只有点的班（forced）和远征中/待收；preset 死班表不再画出来。
const displayedExpeditionBlocks = expeditionBlocks

/** 远征建议淡影：投进对应队伍的子泳道，点采纳 = 那班记 forced */
const expeditionSuggestionBlocks = computed(() => {
  if (!data.value) return []
  return (data.value.expedition_suggestions || []).map((s) => {
    const team = TEAM_NAMES[s.team_no] ?? String(s.team_no)
    const visibleDuration = Math.min(Math.max(s.duration_min, 10), DAY - s.start_min)
    const range = `${fmtMin(s.start_min)}–${fmtMin(Math.min(DAY, s.start_min + s.duration_min))}`
    return {
      key: s.key,
      suggestion: s,
      teamNo: s.team_no,
      left: pct(s.start_min),
      width: Math.max(pct(visibleDuration), 0.7),
      title: `建议：部队${team} ${s.map_code}（${range}）· ${s.reason} · 点击配置或启用`,
      text: s.map_code,
    }
  })
})

/** 远征按队伍分跑道：可丢队伍 ∪ 有班的队 ∪ 有建议的队；其余整泳道隐藏 */
const expeditionLanes = computed(() => {
  if (!data.value) return []
  const teams = new Set<number>(data.value.expedition_help?.available_teams ?? [])
  displayedExpeditionBlocks.value.forEach(b => teams.add(b.slot.team_no))
  expeditionSuggestionBlocks.value.forEach(s => teams.add(s.teamNo))
  return [...teams].sort((a, b) => a - b).map(team => ({
    team,
    label: `远征·${TEAM_NAMES[team] ?? team}`,
    blocks: displayedExpeditionBlocks.value.filter(b => b.slot.team_no === team),
    suggestions: expeditionSuggestionBlocks.value.filter(s => s.teamNo === team),
  }))
})

const runBlocks = computed(() => {
  if (!data.value) return []
  const dayStart = data.value.day_start
  return data.value.runs.map((r, i) => {
    const start = Math.min(DAY, Math.max(0, ((r.started_at - dayStart) / 60)))
    const end = r.ended_at
      ? Math.min(DAY, Math.max(start, (r.ended_at - dayStart) / 60))
      : nowMin.value
    const range = r.ended_at
      ? `${fmtTs(r.started_at)}–${fmtTs(r.ended_at)}`
      : r.tone === 'running'
        ? `${fmtTs(r.started_at)}–进行中`
        : `${fmtTs(r.started_at)}–记录中断`
    return {
      key: `${r.script}-${i}`,
      minute: start,
      left: pct(start),
      width: Math.max(pct(Math.max(end - start, 4)), 0.7),
      cls: `is-${r.tone}`,
      title: `${r.label} ${range} · ${TONE_LABELS[r.tone] ?? r.status}`,
      text: r.label,
      rowTitle: r.label,
      rowDetail: `${range} · ${TONE_LABELS[r.tone] ?? r.status}`,
      time: fmtTs(r.started_at),
      tone: `is-${r.tone}`,
      current: r.tone === 'running',
    }
  })
})

const suggestionBlocks = computed(() => {
  const suggestions = data.value?.suggestions
  if (!suggestions) return []
  // 已排联队战就展示正式安排；改过时间的安排也属于已采纳。
  const hasRaid = (editing.value ? draft.value : data.value?.booking?.blocks || [])
    .some(row => row.kind === 'raid' || (row.kind === 'activity' && row.script === 'raid'))
  return suggestions.filter(s => s.runs == null || !hasRaid).map((s, i) => {
    const range = `${fmtMin(s.start_min)}–${fmtMin(s.start_min + s.duration_min)}`
    const activityLabel = s.runs != null ? `联队战 ${s.runs} 圈` : '挂机建议'
    const detail = `${s.runs != null ? `${s.runs} 圈 · ` : '挂 '}${durationText(s.duration_min)}${s.note ? ` · ${s.note}` : ''}`
    return {
      key: `suggest-${i}`,
      minute: s.start_min,
      runs: s.runs ?? undefined,
      left: pct(s.start_min),
      width: Math.max(pct(Math.max(s.duration_min, 4)), 0.7),
      suggestionIndex: data.value!.suggestions!.indexOf(s),
      cls: 'tlx-suggest',
      title: `建议：${activityLabel} · ${range} · ${durationText(s.duration_min)}${s.note ? ` · ${s.note}` : ''} · 点击配置或启用`,
      text: s.runs != null ? `建议 ${s.runs} 圈` : '建议',
      rowTitle: activityLabel,
      rowDetail: `${range} ${detail}`,
      time: fmtMin(s.start_min),
      tone: 'is-suggest',
      current: false,
      durationMin: s.duration_min,
    }
  })
})

/** 编辑中的草稿已采纳的建议不再显示淡影 */
const suggestionShadows = computed(() => {
  const shadows = suggestionBlocks.value
  if (!editing.value) return shadows
  const taken = new Set(draft.value
    .filter(row => row.kind === 'raid')
    .map(row => parseTime(row.time))
    .filter((value): value is number => value != null))
  return shadows.filter(shadow => !taken.has(shadow.minute))
})

interface ScheduleLaneBlock {
  key: string
  index: number
  block: DayScheduleBlock
  lane: 'task' | 'daily'
  left: number
  width: number
  cls: string
  title: string
  text: string
}

const scheduleBlocks = computed<ScheduleLaneBlock[]>(() => {
  const booking = data.value?.booking
  if (!booking) return []
  const stale = !data.value?.conductor.enabled && booking.issues.length > 0
  return booking.blocks.map((block, index) => {
    const end = blockEndMin(block) ?? block.start_min + (block.kind === 'workflow' ? 4 : GENERIC_BLOCK_MIN)
    const cblock = conductorBlockFor(block)
    const lane = 'task' as const
    return {
      key: `booked-${index}`,
      index,
      block,
      lane,
      left: pct(block.start_min),
      width: Math.max(pct(Math.max(end - block.start_min, 4)), 0.7),
      cls: cblock ? blockStatusClass(cblock) : stale ? 'is-stale' : block.kind === 'daily' ? 'tlx-daily' : 'tlx-task',
      title: `${fmtMin(block.start_min)} ${blockLabel(block)}${cblock ? ` · ${blockStatusText(cblock)}` : ' · 点我改参数/移出'}`,
      text: block.kind === 'raid' ? `${block.runs} 圈` : block.kind === 'activity' ? `${gameplayLabel(block.script)} ${block.runs} 次` : block.kind === 'daily' ? '日课' : '任务流',
    }
  })
})

const taskBlocks = computed(() => scheduleBlocks.value.filter(block => block.lane === 'task'))

const bookedRows = computed(() => (data.value?.booking?.blocks || []).map((block, index) => ({
  key: index,
  block,
  cblock: conductorBlockFor(block),
})))

const bookingSummary = computed(() => {
  const booking = data.value?.booking
  if (!booking) return ''
  const raidRuns = booking.blocks
    .filter(block => block.kind === 'raid')
    .reduce((sum, block) => sum + (block.runs || 0), 0)
  const bits = [`安排了 ${booking.blocks.length} 段`]
  if (raidRuns) bits.push(`共 ${raidRuns} 圈`)
  if (data.value?.conductor.enabled) bits.push('大总管已接班，到点自动开工')
  else if (booking.issues.length) bits.push('需要重看')
  else bits.push('只记计划，尚未开启自动开工')
  return bits.join(' · ')
})

const shortfallText = computed(() => {
  const shortfall = data.value?.shortfall_seconds
  if (!shortfall || shortfall <= 0) return ''
  const activity = data.value?.activity
  if (activity) return `这张时间表在次日 04:00 前可安排 ${activity.planned_runs} / ${activity.target_runs} 圈，还差 ${activity.target_runs - activity.planned_runs} 圈排不下。`
  const covered = suggestionBlocks.value.reduce((sum, b) => sum + b.durationMin, 0)
  const lacking = Math.ceil(shortfall / 60)
  return `今天空窗只够约 ${durationText(covered)}，还差约 ${durationText(lacking)} 排不下。`
})

const compactRows = computed(() => {
  const expeditions = displayedExpeditionBlocks.value.map((b) => ({
    key: `exp-${b.key}`,
    minute: b.minute,
    time: b.time,
    title: b.rowTitle,
    detail: b.rowDetail,
    tone: b.tone,
    current: false,
    enabled: b.enabled,
  }))
  const runs = runBlocks.value.map((b) => ({
    key: `run-${b.key}`,
    minute: b.minute,
    time: b.current ? '现在' : b.time,
    title: b.rowTitle,
    detail: b.rowDetail,
    tone: b.tone,
    current: b.current,
    enabled: true,
  }))
  // 近期活动只收实际执行的记录，待执行安排和建议留在安排区。
  const rows = [...expeditions.filter(row => {
    const item = data.value?.expeditions.find(e => `exp-${e.key}` === row.key)
    return item && ['dispatched', 'running', 'awaiting_collect', 'failed_unknown'].includes(item.state)
  }), ...runs]
  return rows.sort((a, b) => Number(b.current) - Number(a.current) || b.minute - a.minute)
    .slice(0, COMPACT_LIMIT)
})

const compactHeading = '近期活动'

const showDetails = computed(() => !props.collapsible || expanded.value)

const caption = computed(() => {
  const running = runBlocks.value.find((block) => block.current)
  if (running) return `正在跑 ${running.rowTitle}`
  const activity = data.value?.activity
  if (activity) {
    if (!activity.target_runs) return '联队战今天的进度已够，等下一次记账再更新'
    return `联队战接下来建议 ${activity.target_runs} 圈 · 已找到 ${activity.planned_runs} 圈的空窗`
  }
  const nextEnabled = expeditionBlocks.value
    .filter((block) => block.minute >= nowMin.value && block.enabled)
    .sort((a, b) => a.minute - b.minute)[0]
  if (nextEnabled) return `下一班 ${nextEnabled.time} · ${nextEnabled.rowTitle}`
  if (expeditionBlocks.value.length && !expeditionBlocks.value.some((block) => block.enabled)) {
    return '今天点上的远征班都已过点或取消'
  }
  return '点绿色建议配置远征，保存后安排出发'
})
</script>

<template>
  <PaperCard variant="dashboard" class="timeline-card" :class="{ 'is-collapsible': props.collapsible, 'is-collapsed': props.collapsible && !expanded }">
    <div class="tl-card-head">
      <div>
        <h3><span aria-hidden="true">◷</span> 今天的时间表</h3>
        <p>{{ caption }}</p>
      </div>
      <div class="tl-card-actions">
        <small v-if="refreshMessage" role="status">{{ refreshMessage }}</small>
        <time v-if="data">{{ fmtMin(nowMin) }}</time>
        <button type="button" :disabled="refreshing || !!drag || dragBusy" @click="refreshTimeline">{{ refreshing ? '刷新中…' : '刷新' }}</button>
        <button v-if="props.collapsible" type="button" :aria-expanded="expanded" @click="expanded = !expanded">
          {{ expanded ? '收起' : '展开' }}
        </button>
      </div>
    </div>
    <template v-if="data && showDetails">
      <div class="tl-chart tl-wide">
        <output v-if="drag?.moved" class="tl-drag-time">{{ fmtMin(drag.minute) }}<template v-if="dragEnd != null">–{{ fmtMin(dragEnd) }}</template> · 松手调整</output>
        <div class="tl-ticks">
          <span v-for="t in TICKS" :key="t" class="tl-tick" :class="{ 'tl-tick-end': t === DAY }" :style="{ left: pct(t) + '%' }">{{ tickLabel(t) }}</span>
        </div>
        <div class="tl-axis">
          <span v-for="t in TICKS" :key="`g${t}`" class="tl-gridline" :style="{ left: pct(t) + '%' }"></span>
          <div v-for="m in data.markers" :key="m.kind" class="tl-marker" :style="{ left: pct(m.time_min) + '%' }">
            <i>{{ m.label }}</i>
          </div>
          <div class="tl-now" :style="{ left: pct(smoothNowMin) + '%' }"></div>
          <template v-if="expeditionLanes.length">
            <div v-for="lane in expeditionLanes" :key="lane.team" class="tl-lane tl-sub-lane">
              <span class="tl-lane-tag">{{ lane.label }}</span>
              <button v-for="b in lane.blocks" :key="b.key" type="button" class="tl-block" :class="b.cls" :style="{ left: dragLeft(b.key, b.left) + '%', width: b.width + '%' }" :title="b.title" :aria-label="`${b.time} ${b.rowTitle}，${STATE_LABELS[b.slot.state] ?? b.slot.state}${b.slot.toggleable ? '，点击配置这班' : ''}`" aria-haspopup="true" :disabled="!b.slot.toggleable || !!togglingExpedition" @pointerdown="startDrag($event, b.key, 'expedition', b.minute)" @pointermove="moveDrag" @pointerup="finishDrag" @pointercancel="finishDrag" @click="!suppressClick && openExpeditionPopup(lane.team, b.left, undefined, b.slot)">{{ b.text }}</button>
              <button v-for="s in lane.suggestions" :key="`suggest-${s.key}`" type="button" class="tl-block tlx-suggest" :style="{ left: dragLeft(s.key, s.left) + '%', width: s.width + '%' }" :title="s.title" :disabled="!!adoptingSuggestion || prefsBusy" @pointerdown="startDrag($event, s.key, 'expeditionSuggestion', s.suggestion.start_min)" @pointermove="moveDrag" @pointerup="finishDrag" @pointercancel="finishDrag" @click="!suppressClick && openExpeditionPopup(lane.team, s.left, s.suggestion)">{{ s.text }}</button>
              <div v-if="expeditionPopup?.team === lane.team" class="tl-popover" :style="{left: popoverLeft(expeditionPopup?.left || 0)}">
                <strong>部队{{ TEAM_NAMES[lane.team] }}远征 {{ expeditionPopup?.suggestion?.map_code || expeditionPopup?.slot?.map_code }}</strong>
                <div class="tl-popover-actions"><button type="button" :disabled="!!adoptingSuggestion || !!togglingExpedition" @click="configureExpedition">配置</button><button type="button" :disabled="!!adoptingSuggestion || !!togglingExpedition" @click="removeExpeditionFromPopup">{{ expeditionPopup.suggestion || !expeditionPopup.slot?.will_run ? (adoptingSuggestion || togglingExpedition ? '启用中…' : '启用') : '移除' }}</button></div>
              </div>
            </div>
          </template>
          <div v-else class="tl-lane">
            <span class="tl-lane-tag">远征</span>
            <span class="tl-lane-empty">今天没有远征安排</span>
          </div>
          <div class="tl-lane">
            <span class="tl-lane-tag">任务</span>
            <button v-for="b in taskBlocks" :key="b.key" type="button" class="tl-block" :class="b.cls" :style="{ left: dragLeft(b.key, b.left) + '%', width: b.width + '%' }" :title="b.title"  :data-task-index="b.index" @pointerdown="startDrag($event, b.key, 'task', b.block.start_min, b.index)" @pointermove="moveDrag" @pointerup="finishDrag" @pointercancel="finishDrag" @click="openLaneTask($event, b.index)">{{ b.text }}</button>
            <button v-for="b in suggestionShadows" :key="b.key" type="button" class="tl-block tlx-suggest" :style="{ left: dragLeft(b.key, b.left) + '%', width: b.width + '%' }" :title="b.title" :data-task-index="-1" @pointerdown="startDrag($event, b.key, 'suggestion', b.minute, b.suggestionIndex)" @pointermove="moveDrag" @pointerup="finishDrag" @pointercancel="finishDrag" :data-suggestion-key="b.key" @click="openLaneTask($event, -1, b)">{{ b.text }}</button>
            <span v-if="!taskBlocks.length && !suggestionShadows.length" class="tl-lane-empty">还没排要跑的活</span>
            <div v-if="popover && popover.origin !== 'booking' && popover.lane === 'task' && popoverBlock" class="tl-popover" :style="{ left: popoverLeft(popover.left) }">
              <section v-for="entry in overlappingTasks.length ? overlappingTasks : [{index: popover.index, block: popoverBlock}]" :key="`${entry.index}:${entry.block.start_min}`" class="tl-popover-task">
                <strong>{{ blockLabel(entry.block) }}</strong>
                <small>{{ fmtMin(entry.block.start_min) }}<template v-if="blockEndMin(entry.block) != null"> – {{ fmtMin(blockEndMin(entry.block)!) }}</template> 开工</small>
                <small v-if="conductorBlockFor(entry.block)" class="tl-status" :class="blockStatusClass(conductorBlockFor(entry.block)!)">{{ blockStatusText(conductorBlockFor(entry.block)!) }}</small>
                <div class="tl-popover-actions">
                  <button type="button" :disabled="removing" @click="configureLaneTask(entry)">配置</button>
                  <button type="button" :disabled="removing" @click="actOnLaneTask(entry)">{{ entry.index === -1 ? (removing ? '启用中…' : '启用') : (removing ? '移除中…' : '移除') }}</button>
                </div>
              </section>
            </div>
          </div>

        </div>
      </div>
      <div v-if="data.expedition_help" class="tl-expedition-help">
        <label class="tl-expedition-help-count" title="包含今天已出发和已安排的班次。多班会接在前班归来、收菜后；跑够次数就不再推荐。">每支勾选部队今天共安排
          <select :value="data.expedition_help.rounds_per_team" :disabled="prefsBusy || !!adoptingSuggestion" @change="updateRounds">
            <option v-for="n in [0, 1, 2, 3, 4, 5]" :key="n" :value="n">{{ n }}</option>
          </select>
        次远征</label>
        <span class="tl-expedition-help-teams">队伍
          <button v-for="t in [1, 2, 3, 4, 5]" :key="t" type="button" class="tlx-chip" :class="{ 'is-on': data.expedition_help.available_teams.includes(t) }" :aria-pressed="data.expedition_help.available_teams.includes(t)" :disabled="prefsBusy || !!adoptingSuggestion" @click="toggleAvailableTeam(t)">{{ TEAM_NAMES[t] }}</button>
        </span>

      </div>
      <p v-if="expeditionMessage" class="tl-expedition-message" role="status">{{ expeditionMessage }}</p>
      <div class="tl-compact">
        <div class="tl-mini-meta">
          <span><i class="is-expedition"></i>远征 <i class="is-task"></i>任务<template v-if="suggestionBlocks.length"> <i class="is-suggest"></i>建议</template></span>
          <span>04:00 日课刷新</span>
        </div>
        <div class="tl-mini-axis" aria-label="今天至次日凌晨的时间概览">
          <span v-for="t in MINI_TICKS.slice(1, -1)" :key="`mini-grid-${t}`" class="tl-mini-grid" :style="{ left: pct(t) + '%' }"></span>
          <span class="tl-mini-reset" :style="{ left: pct(1680) + '%' }" title="04:00 日课刷新"></span>
          <span class="tl-mini-now" :style="{ left: pct(smoothNowMin) + '%' }" title="现在"></span>
          <span v-for="b in displayedExpeditionBlocks" :key="`mini-${b.key}`" class="tl-mini-block is-expedition" :class="b.cls" :style="{ left: dragLeft(b.key, b.left) + '%', width: b.width + '%' }" :title="b.title"></span>
          <span v-for="b in scheduleBlocks" :key="`mini-${b.key}`" class="tl-mini-block is-task" :class="b.cls" :style="{ left: dragLeft(b.key, b.left) + '%', width: b.width + '%' }" :title="b.title"></span>
          <span v-for="b in runBlocks" :key="`mini-${b.key}`" class="tl-mini-block is-task" :class="b.cls" :style="{ left: dragLeft(b.key, b.left) + '%', width: b.width + '%' }" :title="b.title"></span>
          <span v-for="b in suggestionShadows" :key="`mini-${b.key}`" class="tl-mini-block is-suggest" :style="{ left: dragLeft(b.key, b.left) + '%', width: b.width + '%' }" :title="b.title"></span>
          <span v-for="s in expeditionSuggestionBlocks" :key="`mini-suggest-${s.key}`" class="tl-mini-block is-suggest" :style="{ left: dragLeft(s.key, s.left) + '%', width: s.width + '%' }" :title="s.title"></span>
        </div>
        <div class="tl-mini-ticks">
          <span v-for="t in MINI_TICKS" :key="`mini-tick-${t}`" :style="{ left: pct(t) + '%' }" :class="{ 'is-end': t === DAY }">{{ t >= 1440 ? `${t === 1440 ? '次日 ' : ''}${t / 60 - 24}时` : `${t / 60}时` }}</span>
        </div>
        <div v-if="compactRows.length" class="tl-agenda">
          <strong class="tl-agenda-title">{{ compactHeading }}</strong>
          <div v-for="row in compactRows" :key="row.key" class="tl-agenda-row" :class="{ 'is-current': row.current }">
            <time>{{ row.time }}</time>
            <span class="tl-agenda-dot" :class="row.tone"></span>
            <span class="tl-agenda-copy"><b>{{ row.title }}</b><small>{{ row.detail }}</small></span>
          </div>
        </div>
        <p v-else class="empty">今天还没有执行记录</p>
      </div>

      <p v-if="shortfallText" class="tl-shortfall">{{ shortfallText }}</p>
      <section v-if="data.workflow_active" class="tl-booking" aria-label="正在执行的任务流">
        <strong>正在执行 · {{ data.workflow_active.name }}</strong>
        <ol class="tl-flow-steps">
          <li v-for="(step, index) in data.workflow_active.steps" :key="index"><span>{{ step.wait_time ? `等待至 ${step.wait_time} 再继续` : step.at != null ? clockAt(step.at) : '前一步结束后' }}</span><b v-if="!step.wait_time">{{ step.label }}</b></li>
        </ol>
      </section>
      <section v-for="waiting in data.workflow_waits || []" :key="waiting.id" class="tl-booking" :aria-label="`${waiting.name}的后续安排`">
        <strong>{{ waiting.name }}</strong>
        <p class="tl-booking-message">{{ waiting.status === 'waiting' ? `等待至 ${clockAt(waiting.wake_at)}；到点排队继续，其他任务可以开工。` : waiting.reason || '正在继续后面的步骤' }}</p>
        <ol v-if="waiting.steps.length" class="tl-flow-steps">
          <li v-for="(step, index) in waiting.steps" :key="index"><span>{{ step.wait_time ? `等待至 ${step.wait_time} 再继续` : step.at != null ? clockAt(step.at) : '前一步结束后' }}</span><b v-if="!step.wait_time">{{ step.label }}</b></li>
        </ol>
        <button v-if="waiting.status === 'waiting' || waiting.status === 'interrupted'" type="button" class="tl-booking-link" @click="cancelWorkflowWait(waiting.id)">取消后续步骤</button>
      </section>
      <section class="tl-booking" aria-label="今日安排">
        <div class="tl-booking-head">
          <div>
            <strong>今日安排</strong>
            <small v-if="data.booking">{{ bookingSummary }}</small>
          </div>
          <div class="tl-booking-actions">
            <button v-if="!editing" type="button" :disabled="saving || !firstGameplay" @click="addGameplayConfiguration">＋ 安排玩法</button>
            <button v-if="!editing" type="button" :disabled="saving" @click="addTimedWorkflow">＋ 安排任务流</button>
          </div>
        </div>
        <p v-if="data.expedition_advice_note && !data.expedition_advice_note.includes('不用再点')" class="tl-expedition-help-note" role="status">{{ data.expedition_advice_note }}</p>
        <details v-for="s in (data.expedition_suggestions || []).filter(s => s.blocked_resource && s.restrictions?.length)" :key="`restriction-${s.key}`" class="tl-expedition-help-note">
          <summary>{{ s.blocked_resource }}暂时排不出，部队{{ TEAM_NAMES[s.team_no] ?? s.team_no }}改补{{ s.resource }} · 查看限制</summary>
          <p v-for="reason in s.restrictions" :key="reason">{{ reason }}</p>
          <p v-if="s.formation_change">想优先补{{ s.blocked_resource }}，先在游戏里调整部队{{ TEAM_NAMES[s.team_no] ?? s.team_no }}，同步近况后再看推荐。</p>
          <p v-else>换个出发时间，或等远征图空出来，再看推荐。</p>
        </details>
        <p v-if="data.booking?.issues.length && !data.conductor.enabled" class="tl-booking-warning">{{ data.booking.issues.join('；') }}。请重新安排。</p>
        <p v-if="!data.conductor.available && !editing" class="tl-booking-message">纯净账房只记安排；自动开工需在自动化面板开启。</p>
        <div v-if="data.booking && !editing" class="tl-booked-list">
          <div v-for="row in bookedRows" :key="row.key" class="tl-booked-flow">
          <button type="button" class="tl-booking-link" @click="openBlockPopover({index: data.booking!.blocks.indexOf(row.block), lane: 'task', left: pct(row.block.start_min), origin: 'booking'})">
            {{ fmtMin(row.block.start_min) }} · {{ blockLabel(row.block) }}
            <template v-if="row.cblock"> · <small class="tl-status" :class="blockStatusClass(row.cblock)">{{ blockStatusText(row.cblock) }}</small></template>
            <template v-else-if="row.block.kind === 'raid' && blockEndMin(row.block) != null"> · 预计 {{ fmtMin(blockEndMin(row.block)!) }} 收工</template>
          </button>
          <div v-if="popover?.origin === 'booking' && popover.index === data.booking!.blocks.indexOf(row.block)" class="tl-popover tl-booking-popover">
            <strong>{{ blockLabel(row.block) }}</strong>
            <div class="tl-popover-actions"><button type="button" @click="editFromPopover">配置</button><button type="button" :disabled="removing" @click="removeFromSchedule">移除</button></div>
          </div>
          <details v-if="row.block.steps?.length" class="tl-booked-steps">
          <summary>查看步骤</summary>
          <ol class="tl-flow-steps" aria-label="任务流的执行顺序">
            <li v-for="(step, index) in row.block.steps" :key="index"><span>{{ step.wait_time ? `等待至 ${step.wait_time} 再继续` : step.at != null ? clockAt(step.at) : '前一步结束后' }}</span><b v-if="!step.wait_time">{{ step.label }}</b></li>
          </ol>
          </details>
          </div>
        </div>
        <dialog v-if="editing" ref="planEditorDialog" class="tl-plan-dialog tl-booking-editor" @cancel="editing = false">
          <div v-for="(row, index) in draft" :key="index" class="tl-booking-row" :class="{ 'is-highlight': index === highlightIndex }">
            <label>开始时间 <input v-model="row.time" type="time" step="60" /></label>
            <label v-if="row.kind === 'raid' || row.kind === 'activity'">玩法
              <select v-model="row.script" @change="chooseGameplay(row)">
                <option v-for="option in gameplayOptions" :key="option.script" :value="option.script" :disabled="!option.available">{{ option.label }}{{ option.available ? '' : '（未开放）' }}</option>
              </select>
            </label>
            <span v-else-if="row.kind !== 'workflow'">一键日课（旧安排）</span>
            <label v-if="row.kind === 'raid' || row.kind === 'activity'">次数 <input v-model.number="row.runs" type="number" min="1" max="99" step="1" inputmode="numeric" /></label>
            <label v-if="row.kind === 'workflow'">任务流
              <select v-model="row.workflow_id">
                <option value="" disabled>选一份任务流</option>
                <option v-for="preset in workflowPresets" :key="preset.id" :value="preset.id">{{ preset.name }}</option>
              </select>
            </label>
            <button v-if="row.kind === 'raid' || row.kind === 'activity'" type="button" class="tl-booking-link" @click="gameplayDialog?.open(row.script)">玩法设置</button>
            <span v-if="row.kind === 'activity'">暂无圈速，暂留 30 分钟；前段收工后再开工</span>
            <span v-if="row.kind === 'raid' && !raidKindAvailable" class="tl-booking-warning-inline">联队战还没开，这段请移除或换成别的活</span>
            <span v-else-if="rowEndText(row)">预计 {{ rowEndText(row) }} 收工</span>
            <ol v-if="row.kind === 'workflow' && presetSteps(row.workflow_id).length" class="tl-flow-steps" aria-label="任务流预览">
              <li v-for="(step, stepIndex) in presetSteps(row.workflow_id)" :key="stepIndex"><span>{{ step.wait_time ? `等待至 ${step.wait_time} 再继续` : stepIndex === 0 ? '开始后' : '前一步结束后' }}</span><b v-if="!step.wait_time">{{ step.label }}</b></li>
            </ol>
          </div>
          <p v-if="draft.some(row => row.kind === 'raid' || row.kind === 'activity')" class="tl-booking-message">部队、换队长和补充手形等使用已保存的玩法设置；次数以本段安排为准。</p>
          <p v-if="preview.issues.length" class="tl-booking-warning">{{ preview.issues.join('；') }}</p>
          <div class="tl-booking-actions">
            <button type="button" :disabled="saving || preview.issues.length > 0" @click="saveSchedule">{{ saving ? '保存中…' : '保存' }}</button>
            <button type="button" :disabled="saving" @click="editing = false; highlightIndex = -1; planMessage = ''">取消</button>
          </div>
          <p v-if="planMessage" class="tl-booking-message" role="status">{{ planMessage }}</p>
        </dialog>
        <p v-if="planMessage" class="tl-booking-message" role="status">{{ planMessage }}</p>
        <p v-if="data.conductor.issues.length && !editing" class="tl-booking-warning">{{ [...new Set(data.conductor.issues)].join('；') }}</p>
        <div v-for="block in data.conductor.blocks.filter(b => b.recovery)" :key="block.run_id" class="tl-expedition-help-note">
          <strong>联队战中断了 · 已完成 {{ block.recovery!.completed }}/{{ block.runs }} 圈</strong>
          <p>先在游戏里回到本丸，再继续。</p>
          <div v-if="block.recovery!.uncertain_round">
            <p>中断的那一圈后来打完了吗？</p>
            <div class="tl-booking-actions"><button type="button" :disabled="conductorBusy || !data.conductor.available" @click="resumeRaid(block, true)">打完了，继续剩余 {{ Math.max(0, block.recovery!.remaining - 1) }} 圈</button>
            <button type="button" :disabled="conductorBusy || !data.conductor.available" @click="resumeRaid(block, false)">没打完，继续剩余 {{ block.recovery!.remaining }} 圈</button></div>
          </div>
          <button v-else type="button" class="tl-booking-link" :disabled="conductorBusy || !data.conductor.available" @click="resumeRaid(block, false)">继续剩余 {{ block.recovery!.remaining }} 圈</button>
        </div>
        <p v-if="conductorMessage" class="tl-booking-message" role="status">{{ conductorMessage }}</p>
      </section>
    </template>
    <p v-else-if="!data" class="empty">时间表加载中…</p>
  </PaperCard>
  <GameplaySettingsDialog ref="gameplayDialog" @saved="gameplaySaved" />
  <dialog ref="expeditionDialog" class="tl-expedition-dialog" @cancel="expeditionConfigBusy && $event.preventDefault()">
    <h3>部队{{ TEAM_NAMES[configuringExpedition?.team || 1] }}远征 {{ configuringExpedition?.suggestion?.map_code || configuringExpedition?.slot?.map_code }}</h3>
    <p v-if="expeditionRewards">获取 {{ expeditionRewards }} <small>（大成功参考）</small></p>
    <p v-else>正在读取远征收益…</p>
    <label>出发时间<input v-model="expeditionTime" type="time" :disabled="expeditionConfigBusy" /></label>
    <small>00:00–03:59 为次日凌晨</small>
    <label>部队编队<select v-model="expeditionFormation" :disabled="expeditionConfigBusy || !expeditionSettings">
      <option value="">保持现有编队</option>
      <option v-if="expeditionFormation && !expeditionPresetOptions.some(p => p.formation_id === expeditionFormation)" :value="expeditionFormation" disabled>原预设不可用，请重新选择</option>
      <option v-for="preset in expeditionPresetOptions" :key="preset.formation_id" :value="preset.formation_id">{{ preset.formation_name }}</option>
    </select></label>
    <label class="tl-expedition-sakura"><input v-model="expeditionSakura" type="checkbox" :disabled="expeditionConfigBusy">出发前补花</label>
    <label v-if="expeditionSakura">补花伤势停止条件<select v-model="expeditionInjury" :disabled="expeditionConfigBusy">
      <option value="light">轻伤</option><option value="medium">中伤</option><option value="heavy">重伤</option>
    </select></label>
    <p v-if="expeditionConfigError" role="alert">{{ expeditionConfigError }}</p>
    <footer><button type="button" :disabled="expeditionConfigBusy || !expeditionSettings" @click="saveExpeditionConfiguration">保存</button><button type="button" :disabled="expeditionConfigBusy" @click="expeditionDialog?.close()">取消</button></footer>
  </dialog>
</template>

<style scoped>
.tl-booking-popover { position: static; margin: 8px 0; }
.tl-plan-dialog { width: min(760px, calc(100vw - 32px)); max-height: calc(100dvh - 40px); overflow: auto; box-sizing: border-box; padding: 24px; border: 1px solid var(--paper-line); border-radius: 14px; color: var(--ink); background: var(--paper-card); }
.tl-plan-dialog::backdrop { background: rgb(0 0 0 / 38%); }
.tl-expedition-dialog { width: min(460px, calc(100vw - 32px)); box-sizing: border-box; padding: 24px; border: 1px solid var(--paper-line); border-radius: 14px; color: var(--ink); background: var(--paper-card); }
.tl-expedition-dialog::backdrop { background: rgb(0 0 0 / 38%); }
.tl-expedition-dialog label { display: flex; flex-direction: column; gap: 8px; margin: 18px 0 8px; }
.tl-expedition-dialog input, .tl-expedition-dialog select, .tl-expedition-dialog button { padding: 9px 12px; font: inherit; color: var(--ink); background: var(--paper-panel); border: 1px solid var(--paper-line); border-radius: 8px; }
.tl-expedition-dialog footer { display: flex; gap: 12px; margin-top: 24px; }
.tl-expedition-dialog .tl-expedition-sakura { flex-direction: row; align-items: center; }

.tl-flow-steps { flex-basis: 100%; margin: 8px 0; padding-left: 22px; color: var(--ink-dim); font-size: 12px; }
.tl-booked-steps { margin-top: 6px; }
.tl-booked-steps summary { width: fit-content; cursor: pointer; color: var(--ink-dim); font-size: 12px; }
.tl-flow-steps li { padding: 4px 0; overflow-wrap: anywhere; }
.tl-flow-steps li span { margin-right: 10px; }
.tl-flow-steps li b { color: var(--ink); font-weight: 500; }
.tl-booked-flow { width: 100%; }

.tl-tick { white-space: nowrap; }
.tl-marker i { left: auto; right: 3px; }
.tl-lane { overflow: visible; }
.tl-mini-axis { overflow: hidden; }
.tl-lane:has(.tl-popover) { overflow: visible; z-index: 50; }
.tl-popover { box-sizing: border-box; width: min(240px, 100%); min-width: 0; }
.tl-mini-ticks { position: relative; height: 14px; }
.tl-block { touch-action: pan-y; }
.tl-drag-time { position: absolute; right: 0; top: -24px; z-index: 60; padding: 3px 8px; background: var(--paper); border: 1px solid var(--line); border-radius: 5px; }
.tl-popover-task { display: grid; gap: 5px; }
.tl-popover-task + .tl-block { touch-action: pan-y; }
.tl-drag-time { position: absolute; right: 0; top: -24px; z-index: 60; padding: 3px 8px; background: var(--paper); border: 1px solid var(--line); border-radius: 5px; }
.tl-popover-task { border-top: 1px solid var(--line); padding-top: 10px; margin-top: 5px; }
.tl-mini-ticks span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
.tl-mini-ticks span:first-child { transform: none; }
.tl-mini-ticks .is-end { transform: translateX(-100%); }
</style>
