<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import ParamField from './ParamField.vue'
import { matchesRule } from '../visibility'
import { compileDaily, dailyRows, listJumpDraft, makeNode, type DailyRow } from './dailySettingsModel'
import type { DayTimeline, WorkflowNode, WorkflowNodeDef, WorkflowPreset } from '../types'
function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T }
const props = defineProps<{timeline: DayTimeline}>()
const emit = defineEmits<{saved: []; openDismantleList: []}>()
const defs = ref<WorkflowNodeDef[]>([])
const rows = ref<DailyRow[]>([])
const preset = ref<WorkflowPreset | null>(null)
const tail = ref<WorkflowNode[]>([])
const selected = ref('signin')
const busy = ref(false)
const loading = ref(true)
const message = ref('')
const after = ref<WorkflowPreset['after']>('none')
const startTime = ref('')
const plannedExpeditions = ref(true)
const running = ref(false)
const activeRow = computed(() => rows.value.find(r => r.id === selected.value))
const activityOptions = computed(() => (props.timeline.gameplay_options || []).filter(o => o.available && o.script !== 'yosari' && o.script !== 'sortie'))
const enabledCount = computed(() => rows.value.filter(r => r.enabled).length)
function definition(type: string) { return defs.value.find(d => d.type === type) }
const activeHasParameters = computed(() => activeRow.value?.nodes.some(node => node.type === 'dismantle' || !!definition(node.type)?.params.length))
const simpleDescription = computed(() => ({signin: '签到加领取万屋的暖心礼包。', naihanka: '开工后安排内番，已经安排过时自动跳过。', rewards: '领取已完成任务的奖励。', inbox: '领取杂物箱里的物品。', sugar: '日课收工前执行炼糖。'}[activeRow.value?.id || ''] || '无需额外设置，开工后会按顺序处理。'))
function valueFor(node: WorkflowNode, key: string) { return node.params[key] ?? definition(node.type)?.params.find(f => f.key === key)?.default }
function summary(row: DailyRow) {
  return row.nodes.map(node => {
    const fields = (definition(node.type)?.params || []).filter(f => f.type !== 'note' && matchesRule(f.visibleWhen, key => valueFor(node, key)))
    return fields.slice(0, 2).map(f => {
      const value = node.params[f.key] ?? f.default
      const option = f.options?.find(o => String(Array.isArray(o) ? o[0] : o) === String(value))
      return `${f.label}：${Array.isArray(option) ? option[1] : typeof value === 'boolean' ? value ? '开启' : '关闭' : value ?? ''}`
    }).join(' · ')
  }).filter(Boolean).join('；') || '无需额外设置'
}
async function load() {
  loading.value = true; message.value = ''
  try {
    const [library, catalog, state] = await Promise.all([api.workflows(), api.workflowNodes(), api.scripts()])
    defs.value = catalog.nodes; running.value = state.running
    preset.value = listJumpDraft.value || library.presets.find(p => p.id === 'builtin-daily') || null
    listJumpDraft.value = null
    if (!preset.value) throw new Error('默认日课没有加载出来，请重试')
    rows.value = dailyRows(preset.value, defs.value, activityOptions.value[0]?.script || '')
    tail.value = clone(preset.value.nodes.filter(n => ['snapshot', 'ledger_sync'].includes(n.type)))
    plannedExpeditions.value = preset.value.daily_ui?.plannedExpeditions !== false
    after.value = preset.value.after || 'none'; startTime.value = preset.value.daily_ui?.startTime || ''
  } catch (err) { message.value = err instanceof Error ? err.message : '日课加载失败' }
  finally { loading.value = false }
}
function changeActivity(event: Event) {
  const type = (event.target as HTMLSelectElement).value
  const row = rows.value.find(r => r.id === 'activity')!
  row.nodes = [makeNode(type, defs.value)]
}
function openDismantleList() {
  if (preset.value) listJumpDraft.value = {...clone(preset.value), nodes: compileDaily(rows.value, tail.value), after: after.value,
    daily_ui: { rows: clone(rows.value), startTime: startTime.value, plannedExpeditions: plannedExpeditions.value }}
  emit('openDismantleList')
}
async function saveExpeditionPrefs(rounds: number, teams: number[]) {
  busy.value = true
  try { await api.setExpeditionHelpPrefs(rounds, teams); emit('saved') }
  catch (err) { message.value = err instanceof Error ? err.message : '远征偏好没有保存成功' }
  finally { busy.value = false }
}
async function setResourceFocus(event: Event) {
  busy.value = true
  try { await api.setExpeditionResourceFocus((event.target as HTMLSelectElement).value); emit('saved') }
  catch (err) { message.value = err instanceof Error ? err.message : '远征偏好没有保存成功' }
  finally { busy.value = false }
}
function setRounds(event: Event) { void saveExpeditionPrefs(Number((event.target as HTMLSelectElement).value), props.timeline.expedition_help.available_teams) }
function toggleTeam(team: number) {
  const teams = props.timeline.expedition_help.available_teams
  void saveExpeditionPrefs(props.timeline.expedition_help.rounds_per_team, teams.includes(team) ? teams.filter(t => t !== team) : [...teams, team].sort())
}
async function save(start = false) {
  if (!preset.value || busy.value) return
  busy.value = true; message.value = ''
  try {
    const current = await api.scripts()
    running.value = current.running
    if (current.running) throw new Error('当前任务收工后再调整日课')
    const nodes = compileDaily(rows.value, tail.value)
    if (!enabledCount.value) throw new Error('先勾选一项日课')
    const activity = rows.value.find(r => r.id === 'activity')
    if (activity?.enabled && (!activity.nodes.length || !activityOptions.value.some(d => d.script === activity.nodes[0]?.type))) throw new Error('当前选择的活动未开放，请更换活动或取消勾选')
    const body: WorkflowPreset = {...preset.value, nodes, after: after.value, daily_mode: true,
      daily_ui: {rows: clone(rows.value), startTime: startTime.value, plannedExpeditions: plannedExpeditions.value}}
    const result = await api.updateWorkflow(body)
    if (!result.ok) throw new Error('日课没有保存成功')
    preset.value = body
    if (start) {
      message.value = (await api.executeToday()).message
    } else message.value = '日课设置已保存'
    emit('saved')
  } catch (err) { message.value = err instanceof Error ? err.message : '保存失败，请重试' }
  finally { busy.value = false }
}
onMounted(load)
async function stopToday() {
  busy.value = true
  try {
    await api.setDayConductor(false)
    await api.stop()
    running.value = false
    message.value = '已叫停今日安排，后续时段不会自动开工。'
    emit('saved')
  } catch (err) { message.value = err instanceof Error ? err.message : '暂时没能叫停，请重试' }
  finally { busy.value = false }
}
</script>

<template>
  <section class="daily-settings" aria-label="一键日课设置">
    <header class="daily-heading"><div><small>本丸执务单</small><h3>今日安排</h3></div><span class="daily-count">日课已选 {{ enabledCount }} 项</span></header>
    <p v-if="loading">正在取出日课设置…</p>
    <section v-if="!loading && preset" class="daily-planned-expeditions">
      <div class="daily-expedition-title"><label><input v-model="plannedExpeditions" type="checkbox" :disabled="busy || running" /> <strong>规划远征</strong></label><details class="daily-help"><summary aria-label="规划远征说明">?</summary><p>先派出到点的远征，再执行日课；后续班次到点接班。已单独开启的远征排班仍按原安排运行。</p></details></div>
      <label>优先攒什么 <select :value="timeline.expedition_help.resource_focus || ''" :disabled="busy || running" @change="setResourceFocus"><option value="">交给狐之助建议</option><option v-for="resource in ['小判','木炭','玉钢','冷却材','砥石','委托符','加速符']" :key="resource" :value="resource">{{ resource }}</option></select></label>
      <label>每队 <select :value="timeline.expedition_help.rounds_per_team" :disabled="busy || running" @change="setRounds"><option v-for="n in [0,1,2,3,4,5]" :value="n" :key="n">{{ n }} 次</option></select></label>
      <div class="daily-team-picker"><span>部队</span> <button v-for="team in [1,2,3,4,5]" :key="team" :aria-pressed="timeline.expedition_help.available_teams.includes(team)" :disabled="busy || running" @click="toggleTeam(team)" :aria-label="`规划远征部队${team}`">{{ ['一','二','三','四','五'][team - 1] }}</button></div>
    </section>

    <div v-if="!loading && preset" class="daily-settings-layout">
      <aside class="daily-checklist"><div class="daily-list-heading"><strong>一键日课</strong><small>从上往下执行</small></div>
        <div v-for="row in rows" :key="row.id" class="daily-check-row" :class="{selected: selected === row.id}">
          <input v-model="row.enabled" type="checkbox" :aria-label="`执行${row.label}`" :disabled="busy || running" />
          <button type="button" :aria-pressed="selected === row.id" @click="selected = row.id"><strong>{{ row.label }}</strong><small v-if="row.id === 'sugar'" class="daily-optional">可选</small></button>
          <button class="daily-gear" type="button" :aria-label="`${row.label}设置`" @click="selected = row.id">⚙</button>
        </div>
        <div class="daily-select-actions"><button :disabled="busy || running" @click="rows.forEach(r => r.enabled = r.id !== 'sugar')">日常全选</button><button :disabled="busy || running" @click="rows.forEach(r => r.enabled = false)">清空</button></div>
      </aside>
      <section v-if="activeRow" class="daily-parameters" :aria-label="`${activeRow.label}参数`">
        <div class="daily-parameter-heading"><h4>{{ activeRow.label }}</h4><small>{{ activeRow.enabled ? '今日执行' : '今日不执行' }}</small></div><p v-if="activeRow.id === 'practice'" class="daily-summary">挑最简单的打，不打丙子椒林剑。</p><p v-else-if="summary(activeRow) !== '无需额外设置'" class="daily-summary">{{ summary(activeRow) }}</p>
        <p v-if="activeRow.id === 'expedition'" class="daily-muted">日课执行到这里时，按指定部队和地图派遣一次；时间表里的远征排班另行安排。</p>
        <p v-if="!activeRow.enabled" class="daily-muted">这项今天没有勾选，仍可提前调整设置。</p>
        <label v-if="activeRow.id === 'activity'" class="daily-activity">活动玩法
          <select :value="activeRow.nodes[0]?.type || ''" :disabled="busy || running" @change="changeActivity">
            <option value="" disabled>选择当期活动</option>
            <option v-if="activeRow.nodes[0] && !activityOptions.some(o => o.script === activeRow!.nodes[0]?.type)" :value="activeRow.nodes[0].type">{{ definition(activeRow.nodes[0].type)?.label || activeRow.nodes[0].type }}（原有安排）</option>
            <option v-for="option in activityOptions" :key="option.script" :value="option.script">{{ option.label }}</option>
          </select>
        </label>
        <p v-if="!activeHasParameters && activeRow.nodes.length" class="daily-muted">{{ simpleDescription }}</p>
        <template v-if="activeHasParameters">
        <fieldset v-for="(node, index) in activeRow.nodes" :key="`${activeRow.id}-${index}-${node.type}`" :disabled="busy || running">
          <legend v-if="activeRow.nodes.length > 1">{{ definition(node.type)?.label || node.type }}</legend>
          <button v-if="node.type === 'dismantle'" type="button" @click="openDismantleList">刀解名单 →</button>
          <div class="daily-fields">
            <template v-for="field in definition(node.type)?.params || []" :key="field.key">
              <ParamField v-if="matchesRule(field.visibleWhen, key => valueFor(node, key))" :field="field" :model-value="node.params[field.key] ?? field.default" @update:model-value="node.params[field.key] = $event" />
            </template>
          </div>
          <p v-if="!definition(node.type)?.params.length" class="daily-muted">无需设置，开工后会按顺序处理。</p>
        </fieldset>
        </template>
        <p v-if="!activeRow.nodes.length">当前没有可用的活动玩法。</p>
      </section>
    </div>


    <footer v-if="preset" class="daily-settings-footer">
      <label>开始时间<input v-model="startTime" type="time" :disabled="busy || running" /><small>留空则现在开始</small></label>
      <label>结束后<select v-model="after" :disabled="busy || running"><option value="none">留在本丸</option><option value="logout">退出游戏</option><option value="shutdown">退出游戏并关闭模拟器</option><option value="sleep">退出游戏、关闭模拟器并休眠电脑</option></select></label>
      <div class="daily-start"><small>已选 {{ enabledCount }} 项 · 最后同步家底</small><div><button class="daily-save" :disabled="busy || running" @click="save(false)">保存设置</button><button class="primary" :disabled="busy || running || !enabledCount" @click="save(true)">{{ busy ? '正在安排…' : running ? '正在执务中' : startTime ? '按时间执行今日安排' : '一键执行今日安排' }}</button></div></div>
    </footer>
    <button v-if="timeline.conductor.enabled || running" class="daily-stop" :disabled="busy" @click="stopToday">{{ running ? '停止当前任务和今日安排' : '停止今日安排' }}</button>
    <p v-if="timeline.booking?.blocks.length" class="daily-muted">开工时将以这份今日安排替换旧的任务时段；远征排班仍保留。</p>
    <p v-if="message" role="status">{{ message }}</p><button v-if="!loading && !preset" @click="load">重新加载</button>
  </section>
</template>

<style scoped>
.daily-settings { --accent: var(--fox-gold); --ink-muted: var(--ink-dim); margin-top: 18px; padding-top: 20px; border-top: 1px dashed var(--paper-line); }
.daily-settings h3, .daily-settings h4, .daily-settings p { margin: 0; }
.daily-heading, .daily-parameter-heading, .daily-list-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.daily-heading h3 { font-size: 24px; margin-top: 5px; }
.daily-heading small { letter-spacing: 2px; }
.daily-count { font-size: 12px; border-bottom: 1px solid var(--paper-line); padding: 6px 0; color: var(--ink-muted); }
.daily-settings small, .daily-muted, .daily-summary { color: var(--ink-muted); }
.daily-planned-expeditions { display: flex; flex-wrap: wrap; align-items: center; gap: 16px 22px; margin: 20px 0 24px; padding: 14px 16px; border: 1px solid var(--paper-line); border-left: 3px solid var(--accent); border-radius: 6px; background: var(--paper-panel); }
.daily-planned-expeditions label, .daily-expedition-title, .daily-team-picker { display: flex; align-items: center; gap: 8px; }
.daily-planned-expeditions label { font-size: 13px; }
.daily-expedition-title { margin-right: auto; position: relative; }
.daily-expedition-title strong { font-size: 15px; }
.daily-settings input[type=checkbox] { accent-color: var(--accent); width: 15px; height: 15px; }
.daily-help { position: static; }
.daily-help summary { cursor: pointer; list-style: none; border: 1px solid var(--paper-line); border-radius: 50%; width: 18px; height: 18px; text-align: center; font-size: 12px; color: var(--ink-muted); }
.daily-help p { position: absolute; z-index: 5; top: 28px; left: 0; width: 240px; max-width: 65vw; padding: 12px; background: var(--paper-panel); border: 1px solid var(--paper-line); box-shadow: 0 4px 12px #0001; font-size: 12px; line-height: 1.7; }
.daily-team-picker { font-size: 13px; }
.daily-team-picker button { border: 1px solid var(--paper-line); border-radius: 50%; width: 28px; height: 28px; padding: 0; color: var(--ink-muted); background: transparent; }
.daily-team-picker button[aria-pressed=true] { background: var(--accent); color: var(--paper-panel); border-color: var(--accent); }
.daily-settings-layout { display: grid; grid-template-columns: minmax(210px, 260px) minmax(0, 1fr); gap: 26px; }
.daily-checklist { min-width: 0; padding: 12px; border: 1px solid var(--paper-line); border-radius: 6px; align-self: start; }
.daily-list-heading { padding: 6px 5px 14px; margin-bottom: 6px; border-bottom: 1px solid var(--paper-line); }
.daily-list-heading small { font-size: 11px; }
.daily-check-row { display: flex; align-items: center; gap: 9px; min-height: 40px; padding: 3px 8px; border-radius: 4px; }
.daily-check-row.selected { background: color-mix(in srgb, var(--fox-gold) 14%, var(--paper-card)); box-shadow: inset 3px 0 var(--accent); }
.daily-check-row input { flex: 0 0 auto; }
.daily-check-row button { display: flex; align-items: center; gap: 8px; background: transparent; border: 0; text-align: left; padding: 4px; min-width: 0; flex: 1; color: inherit; }
.daily-check-row strong { font-size: 14px; font-weight: 500; }
.daily-check-row.selected strong { color: var(--accent); font-weight: 700; }
.daily-optional { font-size: 10px; white-space: nowrap; }
.daily-check-row .daily-gear { flex: 0 0 24px; justify-content: center; font-size: 17px; color: var(--ink-muted); opacity: .65; }
.daily-check-row.selected .daily-gear { opacity: 1; color: var(--accent); }
.daily-select-actions, .daily-start > div { display: flex; gap: 8px; flex-wrap: wrap; }
.daily-select-actions { margin: 12px 0 0; padding-top: 12px; border-top: 1px solid var(--paper-line); }
.daily-select-actions button, .daily-save { background: transparent; color: var(--ink-muted); border: 1px solid var(--paper-line); padding: 7px 12px; border-radius: 5px; }
.daily-parameters { min-width: 0; align-self: start; padding: 22px; background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 3px; box-shadow: 4px 4px 0 var(--paper-line); }
.daily-parameter-heading { padding-bottom: 14px; border-bottom: 1px solid var(--paper-line); margin-bottom: 16px; }
.daily-parameter-heading h4 { font-size: 19px; }
.daily-parameter-heading small { font-size: 11px; white-space: nowrap; }
.daily-settings .daily-summary, .daily-settings .daily-muted { font-size: 12px; line-height: 1.7; margin: 12px 0; }
.daily-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.daily-parameters fieldset { border: 0; padding: 0; margin: 18px 0 0; min-width: 0; }
.daily-parameters legend { margin-bottom: 12px; font-weight: bold; font-size: 13px; }
.daily-settings-footer { display: flex; flex-wrap: wrap; align-items: end; gap: 18px; padding: 18px 0 0; margin-top: 24px; border-top: 1px solid var(--paper-line); }
.daily-settings-footer label, .daily-activity { display: grid; gap: 7px; font-size: 13px; }
.daily-settings-footer small { font-size: 11px; }
.daily-settings-footer input, .daily-settings select { padding: 8px; border: 1px solid var(--paper-line); border-radius: 5px; background: var(--paper-panel); color: inherit; max-width: 100%; }
.daily-start { margin-left: auto; display: grid; gap: 8px; }
.daily-start > small { text-align: right; }
.daily-start .primary { padding: 11px 22px; border-radius: 6px; font-weight: bold; }
.daily-stop { margin-top: 12px; padding: 5px 0; border: 0; background: transparent; color: var(--ink-dim); text-decoration: underline; text-underline-offset: 4px; font-size: 12px; }
@media(max-width: 760px) { .daily-settings-layout, .daily-fields { grid-template-columns: minmax(0, 1fr); } .daily-settings-layout { gap: 18px; } .daily-planned-expeditions { gap: 14px; } .daily-expedition-title { width: 100%; } .daily-start { margin-left: 0; width: 100%; } .daily-start > small { text-align: left; } .daily-parameters { padding: 18px; } }
</style>
