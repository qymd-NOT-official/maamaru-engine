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
    <header><h3>今日安排</h3><p>规划远征 → 一键日课 → 结束后行为；日课参数只用于今日安排。</p></header>
    <p v-if="loading">正在取出日课设置…</p>
    <section v-if="!loading && preset" class="daily-planned-expeditions">
      <h4>① 规划远征</h4>
      <label><input v-model="plannedExpeditions" type="checkbox" :disabled="busy || running" /> 按时间表的建议安排远征</label>
      <p class="daily-muted">先派出到点的远征，再执行日课；后续班次到点接班。已单独开启的远征排班仍按原安排运行。</p>
      <label>优先攒什么 <select :value="timeline.expedition_help.resource_focus || ''" :disabled="busy || running" @change="setResourceFocus"><option value="">交给狐之助建议</option><option v-for="resource in ['小判','木炭','玉钢','冷却材','砥石','委托符','加速符']" :key="resource" :value="resource">{{ resource }}</option></select></label>
      <label>每支部队今天共安排 <select :value="timeline.expedition_help.rounds_per_team" :disabled="busy || running" @change="setRounds"><option v-for="n in [0,1,2,3,4,5]" :value="n" :key="n">{{ n }} 次</option></select></label>
      <div class="daily-select-actions">可用部队 <button v-for="team in [1,2,3,4,5]" :key="team" :aria-pressed="timeline.expedition_help.available_teams.includes(team)" :disabled="busy || running" @click="toggleTeam(team)">部队{{ team }} {{ timeline.expedition_help.available_teams.includes(team) ? '✓' : '' }}</button></div>
    </section>
    <h4 v-if="!loading && preset">② 一键日课</h4>
    <div v-if="!loading && preset" class="daily-settings-layout">
      <aside class="daily-checklist">
        <div v-for="row in rows" :key="row.id" class="daily-check-row" :class="{selected: selected === row.id}">
          <input v-model="row.enabled" type="checkbox" :aria-label="`执行${row.label}`" :disabled="busy || running" />
          <button type="button" :aria-pressed="selected === row.id" @click="selected = row.id"><strong>{{ row.label }}</strong><small>{{ row.id === 'sugar' ? '可选 · 收工前炼糖' : row.enabled ? summary(row) : '今天不执行' }}</small></button>
          <button class="daily-gear" type="button" :aria-label="`${row.label}设置`" @click="selected = row.id">⚙</button>
        </div>
        <div class="daily-select-actions"><button :disabled="busy || running" @click="rows.forEach(r => r.enabled = r.id !== 'sugar')">日常全选</button><button :disabled="busy || running" @click="rows.forEach(r => r.enabled = false)">清空</button></div>
      </aside>
      <section v-if="activeRow" class="daily-parameters" :aria-label="`${activeRow.label}参数`">
        <h4>{{ activeRow.label }}</h4>
        <p v-if="activeRow.id === 'expedition'" class="daily-muted">日课执行到这里时，按指定部队和地图派遣一次；时间表里的远征排班另行安排。</p>
        <p v-if="!activeRow.enabled" class="daily-muted">这项今天没有勾选，仍可提前调整设置。</p>
        <label v-if="activeRow.id === 'activity'" class="daily-activity">活动玩法
          <select :value="activeRow.nodes[0]?.type || ''" :disabled="busy || running" @change="changeActivity">
            <option value="" disabled>选择当期活动</option>
            <option v-if="activeRow.nodes[0] && !activityOptions.some(o => o.script === activeRow!.nodes[0]?.type)" :value="activeRow.nodes[0].type">{{ definition(activeRow.nodes[0].type)?.label || activeRow.nodes[0].type }}（原有安排）</option>
            <option v-for="option in activityOptions" :key="option.script" :value="option.script">{{ option.label }}</option>
          </select>
        </label>
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
        <p v-if="!activeRow.nodes.length">当前没有可用的活动玩法。</p>
      </section>
    </div>
    <p v-if="preset" class="daily-order">执行顺序：{{ rows.filter(r => r.enabled).map(r => r.label).join(' → ') || '尚未勾选' }}</p>
    <h4 v-if="preset" class="daily-ending-title">③ 结束后</h4>
    <footer v-if="preset" class="daily-settings-footer">
      <label>开始时间<input v-model="startTime" type="time" :disabled="busy || running" /><small>留空则现在开始</small></label>
      <label>结束后<select v-model="after" :disabled="busy || running"><option value="none">留在本丸</option><option value="logout">退出游戏</option><option value="shutdown">退出游戏并关闭模拟器</option><option value="sleep">退出游戏、关闭模拟器并休眠电脑</option></select></label>
      <div class="daily-start"><small>已选 {{ enabledCount }} 项 · 最后同步家底</small><div><button :disabled="busy || running" @click="save(false)">保存设置</button><button class="primary" :disabled="busy || running || !enabledCount" @click="save(true)">{{ busy ? '正在安排…' : running ? '正在执务中' : startTime ? '按时间执行今日安排' : '一键执行今日安排' }}</button></div></div>
    </footer>
    <button v-if="timeline.conductor.enabled || running" :disabled="busy" @click="stopToday">{{ running ? '停止当前任务和今日安排' : '停止今日安排' }}</button>
    <p v-if="timeline.booking?.blocks.length" class="daily-muted">开工时将以这份今日安排替换旧的任务时段；远征排班仍保留。</p>
    <p v-if="message" role="status">{{ message }}</p><button v-if="!loading && !preset" @click="load">重新加载</button>
  </section>
</template>

<style scoped>
.daily-settings { margin-top: 24px; padding-top: 20px; border-top: 1px dashed var(--paper-line); }
.daily-planned-expeditions { margin: 20px 0 24px; padding: 16px; border: 1px solid var(--paper-line); border-radius: 8px; }
.daily-planned-expeditions label { display: block; margin-top: 12px; }
.daily-ending-title { margin-top: 24px !important; }
.daily-settings h3, .daily-settings h4 { margin: 0 0 10px; }
.daily-settings header p, .daily-muted, .daily-settings small { color: var(--ink-muted); }
.daily-settings-layout { display: grid; grid-template-columns: minmax(230px, 310px) minmax(0, 1fr); gap: 28px; margin-top: 20px; }
.daily-checklist { min-width: 0; padding: 10px; border: 1px solid var(--paper-line); border-radius: 8px; align-self: start; }
.daily-check-row { display: flex; align-items: center; gap: 8px; padding: 4px 8px; border-radius: 5px; }
.daily-check-row.selected { background: var(--paper-panel); outline: 1px solid var(--paper-line); }
.daily-check-row input { flex: 0 0 auto; accent-color: var(--accent); }
.daily-check-row button { background: transparent; border: 0; text-align: left; padding: 4px; min-width: 0; flex: 1; color: inherit; }
.daily-check-row strong, .daily-check-row small { display: block; }
.daily-check-row small { font-size: 11px; margin-top: 2px; line-height: 1.4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.daily-check-row .daily-gear { flex: 0 0 28px; font-size: 18px; text-align: center; }
.daily-select-actions, .daily-start > div { display: flex; gap: 8px; flex-wrap: wrap; }
.daily-select-actions { margin: 12px 0 0; }
.daily-parameters { min-width: 0; align-self: start; padding: 16px; background: var(--paper-panel); border: 1px solid var(--paper-line); border-radius: 8px; }
.daily-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.daily-parameters fieldset { border: 0; padding: 0; margin: 18px 0; min-width: 0; }
.daily-parameters legend { margin-bottom: 12px; font-weight: bold; }
.daily-settings-footer { display: flex; flex-wrap: wrap; align-items: end; gap: 20px; margin-top: 24px; }
.daily-settings-footer label, .daily-activity { display: grid; gap: 8px; }
.daily-settings-footer input, .daily-settings select { padding: 8px; border: 1px solid var(--paper-line); border-radius: 5px; background: var(--paper-panel); color: inherit; max-width: 100%; }
.daily-start { margin-left: auto; display: grid; gap: 8px; }
@media(max-width: 760px) { .daily-settings-layout, .daily-fields { grid-template-columns: minmax(0, 1fr); } .daily-settings-layout { gap: 16px; } .daily-start { margin-left: 0; width: 100%; } }
</style>
