<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import PaperCard from './PaperCard.vue'
import PixelControl from './PixelControl.vue'
import ParamField from './ParamField.vue'
import SegmentedControl from './SegmentedControl.vue'
import type { ParamField as Field, WorkflowNode, WorkflowNodeCategory, WorkflowNodeDef, WorkflowPreset, WorkflowIdentity } from '../types'
import { matchesRule } from '../visibility'

const props = withDefaults(defineProps<{ embedded?: boolean; running?: boolean; current?: string | null; stopping?: boolean; busy?: boolean; runningWorkflow?: WorkflowIdentity | null; dailyEntry?: number; presetJump?: { id: string; tick: number } | null; active?: boolean }>(), { embedded: false, running: false, current: null, stopping: false, busy: false, runningWorkflow: null, dailyEntry: 0, presetJump: null, active: true })
const emit = defineEmits<{ started: [identity: WorkflowIdentity]; saved: [preset: WorkflowPreset]; stop: []; office: [] }>()
// Keep the draft in the parent so switching pages does not discard edits.
const draft = defineModel<WorkflowPreset | null>('draft', { default: null })
const maxNodes = 30 // panel/workflow.py: MAX_NODES
const categoryOrder: WorkflowNodeCategory[] = ['cold', 'time', 'chore', 'battle']
const categoryLabels: Record<WorkflowNodeCategory, string> = { cold: '准备', time: '进阶', chore: '后勤', battle: '出阵', finish: '收尾' }
const presets = ref<WorkflowPreset[]>([])
const defs = ref<WorkflowNodeDef[]>([])
const expanded = ref<WorkflowNode | null>(null)
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)
const starting = ref(false)
const message = ref('')
const failed = ref(false)
const picker = ref<HTMLDialogElement | null>(null)
const switchDialog = ref<HTMLDialogElement | null>(null)
const searchInput = ref<HTMLInputElement | null>(null)
const moreMenu = ref<HTMLElement | null>(null)
const menuPreset = ref<WorkflowPreset | null>(null)
const menuPosition = ref({ left: '0px', top: '0px' })
const search = ref('')
const category = ref<WorkflowNodeCategory | 'all'>('all')
const insertAt = ref(0)
const lastAdded = ref('')
const removed = ref<{ node: WorkflowNode; index: number } | null>(null)
let handledDailyEntry = 0
let handledPresetJump = 0
let pendingSwitch: (() => void) | null = null
const nodeIds = new WeakMap<WorkflowNode, number>()
let nextId = 0
const locked = computed(() => saving.value || starting.value || props.busy)
const dirty = computed(() => {
  if (!draft.value) return false
  const saved = presets.value.find(preset => preset.id === draft.value?.id)
  return !saved || saved.name !== draft.value.name || canonical(saved.nodes) !== canonical(draft.value.nodes) || (saved.after || 'none') !== (draft.value.after || 'none') || !!saved.daily_mode !== !!draft.value.daily_mode
})
const valid = computed(() => !!draft.value?.name.trim() && !!draft.value?.nodes.length)
const groups = computed(() => categoryOrder
  .filter(item => category.value === 'all' || category.value === item)
  .map(item => ({ category: item, nodes: defs.value.filter(def => def.type !== 'logout' && !def.template_only && def.category === item && `${def.label} ${def.desc}`.toLowerCase().includes(search.value.trim().toLowerCase())) }))
  .filter(group => group.nodes.length))
function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T }
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}
function keyFor(node: WorkflowNode) {
  if (!nodeIds.has(node)) nodeIds.set(node, ++nextId)
  return nodeIds.get(node)!
}
function defOf(type: string) { return defs.value.find(def => def.type === type) }
function valueFor(node: WorkflowNode, key: string) {
  const def = defOf(node.type)
  return node.params[key] ?? def?.params.find(field => field.key === key)?.default ?? ''
}
function description(type: string) {
  if (draft.value?.daily_mode) {
    const dailyDescriptions: Record<string, string> = {
      expedition: '收取归来奖励，按这一步的安排派遣；仍在远征中的部队会跳过。',
      dismantle: '按白名单刀解一把；今天已经刀解过时自动跳过。',
      snapshot: '收工时盘点家底；本轮锻刀已顺手盘点时不再重复。',
    }
    if (dailyDescriptions[type]) return dailyDescriptions[type]
  }
  const descriptions: Record<string, string> = {
    boot_emulator: '打开模拟器，等待开机。模拟器已打开时会跳过。',
    login: '打开并登录游戏，处理登录弹窗，回到本丸。',
    signin: '领取公告中的每日签到奖励。',
  }
  return descriptions[type] ?? defOf(type)?.desc ?? ''
}
function isVisible(field: Field, node: WorkflowNode) {
  return matchesRule(field.visibleWhen, key => valueFor(node, key))
}
function summary(node: WorkflowNode) {
  const fields = (defOf(node.type)?.params || []).filter(field => field.type !== 'note' && isVisible(field, node))
  return fields.slice(0, 3).map(field => {
    const value = valueFor(node, field.key)
    const option = field.options?.find(option => String(Array.isArray(option) ? option[0] : option) === String(value))
    const label = Array.isArray(option) ? option[1] : option
    const display = label ?? (Array.isArray(value) ? value.join('、') : typeof value === 'boolean' ? (value ? '开启' : '关闭') : value)
    return `${field.label?.split(/[（(]/)[0]}：${display === '' || display == null ? '未设置' : display}`
  }).join(' · ') || description(node.type) || '点击查看设置'
}
function tell(text: string, error = false) { message.value = text; failed.value = error }
function closeMenu() { moreMenu.value?.hidePopover() }
async function openMenu(preset: WorkflowPreset, event: MouseEvent) {
  const anchor = (event.currentTarget as HTMLElement).getBoundingClientRect()
  menuPreset.value = preset
  await nextTick()
  moreMenu.value?.showPopover()
  const height = moreMenu.value?.offsetHeight || 80
  menuPosition.value = {
    left: `${Math.max(8, Math.min(anchor.right - 136, window.innerWidth - 144))}px`,
    top: `${anchor.bottom + height + 8 <= window.innerHeight ? anchor.bottom + 4 : Math.max(8, anchor.top - height - 4)}px`,
  }
}
function setDraft(value: WorkflowPreset) {
  draft.value = clone(value)
  closeMenu()
  expanded.value = null
  removed.value = null
  message.value = ''
}
async function load() {
  const keepEdits = dirty.value
  loading.value = true
  loadError.value = ''
  try {
    const [workflowData, nodeData] = await Promise.all([api.workflows(), api.workflowNodes()])
    presets.value = (workflowData.presets || []).filter(preset => preset.id !== 'builtin-daily')
    defs.value = nodeData.nodes || []
    if (draft.value?.id === 'builtin-daily') draft.value = null
    if (!draft.value) setDraft(presets.value[0] || { id: '', name: '', nodes: [] })
    else if (!keepEdits) {
      const current = presets.value.find(preset => preset.id === draft.value?.id)
      if (current && canonical(current) !== canonical(draft.value)) setDraft(current)
    }
    if (props.dailyEntry > handledDailyEntry) { openDaily(); handledDailyEntry = props.dailyEntry }
    if (props.presetJump && props.presetJump.tick > handledPresetJump) { openPresetById(props.presetJump.id); handledPresetJump = props.presetJump.tick }
  } catch (error) {
    loadError.value = error instanceof Error ? `加载失败：${error.message}` : '加载失败，请重试'
  } finally { loading.value = false }
}
function switchTo(action: () => void) {
  if (locked.value) return
  if (dirty.value && (draft.value?.name.trim() || draft.value?.nodes.length)) {
    pendingSwitch = action
    switchDialog.value?.showModal()
  } else action()
}
async function confirmSwitch(saveFirst: boolean) {
  if (saveFirst && !await save()) return
  const action = pendingSwitch
  pendingSwitch = null
  switchDialog.value?.close()
  action?.()
}
function newPreset() { switchTo(() => setDraft({ id: '', name: '', nodes: [] })) }
function select(preset: WorkflowPreset) {
  if (draft.value?.id === preset.id) return
  switchTo(() => setDraft(preset))
}
function openDaily() {
  const daily = presets.value.find(preset => preset.id === 'builtin-daily')
  if (daily) select(daily)
}
// 执务页常用功能里钉住的工作流点「调整」时，按 id 选中对应预设。
function openPresetById(id: string) {
  const preset = presets.value.find(item => item.id === id)
  if (preset) select(preset)
}
watch(() => props.dailyEntry, () => { if (!loading.value) { openDaily(); handledDailyEntry = props.dailyEntry } })
watch(() => props.presetJump, request => {
  if (!request || request.tick <= handledPresetJump) return
  if (!loading.value) { openPresetById(request.id); handledPresetJump = request.tick }
})
watch(() => props.active, active => { if (active) load() })
defineExpose({ dirty, locked })
function duplicate(preset: WorkflowPreset) {
  closeMenu()
  const copy = clone(draft.value?.id === preset.id ? draft.value : preset)
  switchTo(() => setDraft({ ...copy, id: '', name: `${copy.name.slice(0, 27)} 副本` }))
}
async function remove(preset: WorkflowPreset) {
  if (locked.value || props.running) return
  closeMenu()
  if (!window.confirm(`删除「${preset.name}」？这条已保存的流程将无法恢复。`)) return
  saving.value = true
  try {
    const result = await api.deleteWorkflow(preset.id)
    if (!result.ok) throw new Error('没有删除成功，请重试')
    presets.value = presets.value.filter(item => item.id !== preset.id)
    if (draft.value?.id === preset.id) setDraft(presets.value[0] || { id: '', name: '', nodes: [] })
    tell(`已删除「${preset.name}」`)
  } catch (error) { tell(error instanceof Error ? error.message : '删除失败，请重试', true) }
  finally { saving.value = false }
}
function openPicker(index: number) {
  insertAt.value = index
  search.value = ''
  category.value = 'all'
  lastAdded.value = ''
  picker.value?.showModal()
  nextTick(() => searchInput.value?.focus())
}
function addNode(def: WorkflowNodeDef) {
  if (!draft.value || draft.value.nodes.length >= maxNodes) return
  const node: WorkflowNode = { type: def.type, params: clone(Object.fromEntries(def.params.filter(field => field.type !== 'note').map(field => [field.key, field.default ?? '']))), on_error: 'stop' }
  draft.value.nodes.splice(insertAt.value++, 0, node)
  expanded.value = draft.value.nodes[insertAt.value - 1]
  lastAdded.value = `已添加「${def.label}」，可以继续选`
}
function closePicker() {
  picker.value?.close()
  nextTick(() => {
    if (expanded.value) document.getElementById(`wf-step-${keyFor(expanded.value)}`)?.scrollIntoView({ block: 'nearest' })
  })
}
function move(index: number, delta: number) {
  if (!draft.value) return
  const nodes = draft.value.nodes
  const target = index + delta
  if (target < 0 || target >= nodes.length) return
  const [node] = nodes.splice(index, 1)
  nodes.splice(target, 0, node)
  tell(`「${defOf(node.type)?.label || node.type}」已移到第 ${target + 1} 步`)
}
function removeNode(index: number) {
  if (!draft.value) return
  const [node] = draft.value.nodes.splice(index, 1)
  removed.value = { node, index }
  if (expanded.value === node) expanded.value = null
}
function undoRemove() {
  if (!draft.value || !removed.value || draft.value.nodes.length >= maxNodes) return
  draft.value.nodes.splice(Math.min(removed.value.index, draft.value.nodes.length), 0, removed.value.node)
  removed.value = null
}
async function save(): Promise<boolean> {
  if (!draft.value || saving.value || !valid.value) return false
  saving.value = true
  const snapshot = clone(draft.value)
  snapshot.name = snapshot.name.trim()
  try {
    if (snapshot.id) {
      const result = await api.updateWorkflow(snapshot)
      if (!result.ok) throw new Error('没有保存成功，请重试')
    } else {
      const result = await api.createWorkflow({ name: snapshot.name, nodes: snapshot.nodes, after: snapshot.after || 'none', daily_mode: !!snapshot.daily_mode })
      if (!result.ok || !result.id) throw new Error('没有保存成功，请重试')
      snapshot.id = result.id
    }
    // Keep the baseline locally after success: a failed follow-up GET must not
    // make a successful create look failed and duplicate it on retry.
    const index = presets.value.findIndex(item => item.id === snapshot.id)
    if (index >= 0) presets.value[index] = clone(snapshot)
    else presets.value.push(clone(snapshot))
    draft.value.id = snapshot.id
    draft.value.name = snapshot.name
    emit('saved', clone(snapshot))
    tell('流程已保存')
    return true
  } catch (error) {
    tell(error instanceof Error ? `保存失败：${error.message}` : '保存失败，请重试', true)
    return false
  } finally { saving.value = false }
}
async function run() {
  if (props.running || locked.value || !valid.value) return
  starting.value = true
  try {
    if (dirty.value && !await save()) return
    const result = await api.run('workflow', { workflow_id: draft.value!.id })
    if (!result.ok) throw new Error('没有启动成功，请稍后重试')
    emit('started', result.workflow || { id: draft.value!.id, name: draft.value!.name })
    tell(`「${draft.value!.name}」已开始`)
  } catch (error) { tell(error instanceof Error ? `启动失败：${error.message}` : '启动失败，请重试', true) }
  finally { starting.value = false }
}
function protectDraft(event: BeforeUnloadEvent) {
  if (dirty.value && (draft.value?.name.trim() || draft.value?.nodes.length)) {
    event.preventDefault()
    event.returnValue = ''
  }
}
onMounted(() => { load(); window.addEventListener('beforeunload', protectDraft); window.addEventListener('scroll', closeMenu, true) })
onBeforeUnmount(() => { window.removeEventListener('beforeunload', protectDraft); window.removeEventListener('scroll', closeMenu, true) })
</script>

<template>
  <section class="workflow-panel wf-panel">
    <p v-if="loading" class="wf-loading">正在取出你的流程…</p>
    <p v-else-if="loadError" class="wf-loading" role="alert">{{ loadError }} <button class="wf-button" @click="load">重新加载</button></p>
    <div v-else class="wf-layout">
      <aside class="wf-library" aria-label="我的流程">
        <header><h3>我的流程</h3><span>{{ presets.length }}</span></header>
        <button type="button" class="wf-button wf-new" :class="{ selected: !draft?.id }" :disabled="locked" @click="newPreset">＋ 新建流程</button>
        <div class="wf-presets">
          <div v-for="preset in presets" :key="preset.id" class="wf-preset" :class="{ selected: draft?.id === preset.id }">
            <button type="button" class="wf-preset-select" :aria-pressed="draft?.id === preset.id" :disabled="locked" @click="select(preset)">
              <strong>{{ preset.name }}</strong><small>{{ preset.id === 'builtin-daily' ? '默认日课 · ' : '' }}{{ preset.nodes.length }} 个步骤<span v-if="draft?.id === preset.id && dirty"> · 编辑中</span></small>
            </button>
            <button type="button" class="wf-more-button" :aria-label="`${preset.name}的更多操作`" :disabled="locked" aria-haspopup="true" @click="openMenu(preset, $event)"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="3" cy="8" r="1.3" fill="currentColor"/><circle cx="8" cy="8" r="1.3" fill="currentColor"/><circle cx="13" cy="8" r="1.3" fill="currentColor"/></svg></button>
          </div>
        </div>
        <p class="wf-library-note">选一条慢慢调整，<br>也可以从空白开始安排。</p>
      </aside>
      <PaperCard v-if="draft" variant="settings" class="wf-editor">
        <fieldset :disabled="locked" class="wf-edit-fields">
          <header class="wf-editor-head">
            <label class="wf-name">{{ draft.id ? '流程名称' : '新的安排' }}<PixelControl v-model="draft.name" maxlength="30" placeholder="给流程起个名字，比如：晚间日课" /></label>
          </header>
          <p v-if="draft.id === 'builtin-daily'" class="wf-tutorial">这是你的默认日课安排。可以直接运行，也可以从左侧菜单复制一份，试着添加步骤或调整顺序。</p>
          <div class="wf-list-heading"><h3>执行顺序 <span>{{ draft.nodes.length }} / {{ maxNodes }}</span></h3><small>从上往下依次执行</small></div>
          <div v-if="!draft.nodes.length" class="wf-empty">
            <h3>今天想让まあ丸做些什么？</h3><p>先选一个任务，再按你的习惯往下安排。</p>
            <button type="button" class="wf-button wf-primary" @click="openPicker(0)">添加第一个步骤</button>
          </div>
          <div v-else class="wf-steps">
            <template v-for="(node, index) in draft.nodes" :key="keyFor(node)">
              <button type="button" class="wf-insert" :disabled="draft.nodes.length >= maxNodes" :aria-label="`在第 ${index + 1} 步前插入`" @click="openPicker(index)"><span>＋ 在这里插入</span></button>
              <article :id="`wf-step-${keyFor(node)}`" class="wf-step" :class="{ 'is-open': expanded === node }">
                <div class="wf-step-head">
                  <button type="button" class="wf-step-toggle" :aria-expanded="expanded === node" @click="expanded = expanded === node ? null : node">
                    <span class="wf-number">{{ String(index + 1).padStart(2, '0') }}</span>
                    <span class="wf-step-label"><strong>{{ defOf(node.type)?.label || node.type }}<small v-if="node.on_error === 'continue'">失败后继续</small></strong><span>{{ summary(node) }}</span></span>
                    <span class="wf-chevron" aria-hidden="true">{{ expanded === node ? '−' : '＋' }}</span>
                  </button>
                  <span class="wf-step-tools"><button type="button" :aria-label="`上移第 ${index + 1} 步`" title="上移" :disabled="index === 0" @click="move(index, -1)">↑</button><button type="button" :aria-label="`下移第 ${index + 1} 步`" title="下移" :disabled="index === draft.nodes.length - 1" @click="move(index, 1)">↓</button><button type="button" class="wf-danger" :aria-label="`移除第 ${index + 1} 步`" title="移除步骤" @click="removeNode(index)">×</button></span>
                </div>
                <div v-if="expanded === node" class="wf-step-detail">
                  <p>{{ description(node.type) }}</p>
                  <p v-if="defOf(node.type)?.category === 'battle' || node.type === 'expedition'" class="wf-inherited">这里的设置只用于这一步，与单独玩法设置分开保存。</p>
                  <p v-if="node.type === 'raid'" class="wf-inherited">直接运行任务流时，按这里的圈数执行。只有在时间表选「联队战」并指定这份任务流时，才会用时间表填写的圈数替换本次执行；这里的设置不变。</p>
                  <div class="fields wf-params"><ParamField v-for="field in (defOf(node.type)?.params || []).filter(item => isVisible(item, node))" :key="field.key" :field="field" :model-value="valueFor(node, field.key)" @update:model-value="node.params[field.key] = $event" /></div>
                  <p v-if="draft.daily_mode && node.type === 'login'" class="wf-inherited">登录未成功时，会停止后面的日课安排。</p>
                  <div v-else class="wf-error-policy"><span>这一步没完成时</span><SegmentedControl v-model="node.on_error" label="失败后的安排" :items="[{ value: 'stop', label: '停下等我', caption: '暂停后面的安排' }, { value: 'continue', label: '继续下一步', caption: '记录失败，接着执行' }]" /></div>
                </div>
              </article>
            </template>
            <button type="button" class="wf-button wf-add" :disabled="draft.nodes.length >= maxNodes" @click="openPicker(draft.nodes.length)">{{ draft.nodes.length >= maxNodes ? '已达到 30 步上限' : '＋ 添加下一步' }}</button>
          </div>
          <div v-if="removed" class="wf-undo" role="status">已移除「{{ defOf(removed.node.type)?.label || removed.node.type }}」<button type="button" :disabled="draft.nodes.length >= maxNodes" @click="undoRemove">撤销</button></div>
          <div class="wf-finish">
            <label>结束后做什么<PixelControl as="select" :model-value="draft.after || 'none'" :disabled="draft.nodes.some(node => node.type === 'logout')" @update:model-value="draft.after = $event as WorkflowPreset['after']"><option value="none">留在本丸</option><option value="logout">退出游戏</option><option value="shutdown">退出游戏并关闭模拟器</option><option value="sleep">退出游戏、关闭模拟器并休眠电脑</option></PixelControl></label>
            <p v-if="draft.nodes.some(node => node.type === 'logout')">这份旧流程仍有下班步骤。移除它后，就可以在这里统一安排收尾。</p>
            <p v-else>走完最后一步才会执行；中途停止时不会退出游戏或休眠。</p>
          </div>
        </fieldset>
        <footer class="wf-toolbar">
          <span class="wf-save-state" :class="{ unsaved: dirty }">{{ dirty ? '● 尚未保存' : '✓ 已保存' }}</span>
          <div class="wf-actions"><button type="button" class="wf-button" :disabled="!dirty || !valid || locked" @click="save">{{ saving ? '保存中…' : '保存流程' }}</button><button type="button" class="wf-button wf-primary" :disabled="running || locked || !valid" @click="run">{{ starting ? '正在启动…' : dirty ? '保存并运行' : '运行这条' }}<span aria-hidden="true"> →</span></button></div>
        </footer>
        <div v-if="running" class="wf-running"><span>{{ stopping ? '正在停止…' : current === 'workflow' ? (runningWorkflow ? `「${runningWorkflow.name}」正在执行` : '工作流正在执行') : '当前有其他任务在执行，结束后可运行这份安排' }}</span><button v-if="current === 'workflow'" type="button" class="wf-button wf-danger" :disabled="stopping" @click="emit('stop')">{{ stopping ? '正在停止…' : '停止工作流' }}</button><button type="button" class="wf-text-button" @click="emit('office')">去执务台看实况 →</button></div>
        <p v-if="message" class="wf-message" :class="{ 'wf-danger': failed }" :role="failed ? 'alert' : 'status'">{{ message }}</p>
      </PaperCard>
    </div>
    <div ref="moreMenu" popover="auto" class="wf-preset-menu" :style="menuPosition" :aria-label="`${menuPreset?.name || '流程'}的操作`">
      <template v-if="menuPreset">
        <button type="button" :disabled="locked" @click="duplicate(menuPreset)">复制流程</button>
        <button v-if="menuPreset.id !== 'builtin-daily'" type="button" class="wf-danger" :disabled="running || locked" @click="remove(menuPreset)">删除流程</button>
      </template>
    </div>
    <dialog ref="picker" class="wf-dialog wf-picker" aria-labelledby="wf-picker-title" @cancel.prevent="closePicker">
      <header class="wf-dialog-head"><div><h2 id="wf-picker-title">添加步骤</h2><p>可以连续选择，按点击顺序加入流程。</p></div><button type="button" class="wf-close" aria-label="关闭步骤选择" @click="closePicker">×</button></header>
      <div class="wf-search-area"><label class="wf-search"><span aria-hidden="true">⌕</span><input ref="searchInput" v-model="search" type="search" aria-label="搜索步骤" placeholder="搜索任务，比如：江户城、远征" /></label><div class="wf-categories" role="group" aria-label="任务分类"><button type="button" :aria-pressed="category === 'all'" @click="category = 'all'">全部</button><button v-for="item in categoryOrder" :key="item" type="button" :aria-pressed="category === item" @click="category = item">{{ categoryLabels[item] }}</button></div></div>
      <div class="wf-catalog"><section v-for="group in groups" :key="group.category"><h3>{{ categoryLabels[group.category] }}</h3><div class="wf-catalog-grid"><button v-for="def in group.nodes" :key="def.type" type="button" :disabled="(draft?.nodes.length || 0) >= maxNodes" @click="addNode(def)"><span><strong>{{ def.label }}</strong><small>{{ description(def.type) }}</small></span><span class="wf-catalog-plus" aria-hidden="true">＋</span></button></div></section><p v-if="!groups.length" class="wf-no-results">没有找到这个任务，换个关键词试试。</p></div>
      <footer class="wf-picker-footer"><span role="status">{{ (draft?.nodes.length || 0) >= maxNodes ? '已达到 30 步上限' : lastAdded || `将加入第 ${insertAt + 1} 步` }}<small>当前共 {{ draft?.nodes.length || 0 }} 个步骤</small></span><button type="button" class="wf-button wf-primary" @click="closePicker">选好了</button></footer>
    </dialog>
    <dialog ref="switchDialog" class="wf-dialog wf-switch" aria-labelledby="wf-switch-title" @close="pendingSwitch = null">
      <h2 id="wf-switch-title">先收好这份安排？</h2><p>「{{ draft?.name || '未命名流程' }}」还有没保存的修改。</p><p v-if="failed" class="wf-danger" role="alert">{{ message }}</p><div class="wf-switch-actions"><button type="button" class="wf-text-button" :disabled="locked" @click="switchDialog?.close()">继续编辑</button><button type="button" class="wf-button" :disabled="locked" @click="confirmSwitch(false)">放弃修改</button><button type="button" class="wf-button wf-primary" :disabled="!valid || locked" @click="confirmSwitch(true)">{{ saving ? '保存中…' : '保存并切换' }}</button></div>
    </dialog>
  </section>
</template>

<style scoped>
.wf-panel { color: var(--ink); }
/* 顶部工房导航已经说明当前位置；编辑器直接从流程列表和正文开始。 */
.wf-panel button, .wf-dialog button { cursor: pointer; font: inherit; }
.wf-panel button:disabled, .wf-dialog button:disabled { cursor: default; opacity: .42; }
.wf-panel button:focus-visible, .wf-dialog button:focus-visible, .wf-dialog input:focus-visible { outline: 2px solid var(--fox-gold); outline-offset: 3px; }
.wf-layout { display: grid; grid-template-columns: 230px minmax(0, 1fr); gap: 0; align-items: stretch; padding: 0; }
/* 侧栏与系统设置导航同款：贴左缘、自成一列米色带。 */
.wf-library { min-width: 0; padding: 22px 14px; background: #f1e7d6; border-right: 1px solid var(--line); }
.wf-library > header { display: flex; align-items: center; gap: 9px; margin-bottom: 16px; }
.wf-library h3 { margin: 0; font-size: 13px; }
.wf-library > header > span { font-size: 11px; color: var(--ink-dim); }
.wf-button { min-height: 38px; border: 1px solid var(--paper-line); border-radius: 6px; color: var(--ink); background: var(--paper-card); padding: 9px 15px; font-size: 13px; font-weight: 600; }
.wf-button:hover:not(:disabled) { border-color: var(--fox-gold); background: var(--paper-panel); }
.wf-button.wf-primary { background: var(--fox-gold); color: #fffaf0; border-color: var(--fox-gold); }
.wf-button.wf-primary:hover:not(:disabled) { filter: brightness(.95); background: var(--fox-gold); }
.wf-new { width: 100%; text-align: left; background: transparent; border-style: dashed; }
.wf-new.selected { border-color: var(--fox-gold); color: var(--fox-gold); }
.wf-presets { display: grid; gap: 6px; margin-top: 14px; }
.wf-preset { position: relative; text-align: left; border: 1px solid transparent; border-radius: 6px; background: transparent; color: var(--ink); min-width: 0; }
.wf-preset-select { display: block; width: 100%; padding: 12px 38px 12px 12px; text-align: left; border: 0; border-radius: inherit; background: transparent; color: inherit; }
.wf-preset strong { display: block; font-size: 13px; overflow-wrap: anywhere; line-height: 1.6; }
.wf-preset small { display: block; font-size: 11px; color: var(--ink-dim); margin-top: 5px; }
.wf-preset:hover { background: var(--paper-panel); }
.wf-preset.selected { background: var(--paper-card); border-color: var(--paper-line); box-shadow: inset 3px 0 var(--fox-gold); }
.wf-library-note { font-size: 11px; line-height: 1.9; color: var(--ink-dim); margin: 24px 12px; }
.wf-editor { margin: 30px; align-self: start; padding: 0; min-width: 0; border: 1px solid #b8aa96; border-radius: 0; background: #fffaf0; box-shadow: 6px 6px 0 #cfc1aa; overflow: visible; }
.wf-edit-fields { border: 0; margin: 0; padding: 25px 26px 22px; min-width: 0; }
.wf-editor-head { display: flex; gap: 16px; align-items: center; padding-bottom: 24px; }
.wf-tutorial { margin: -8px 0 20px; padding: 12px 14px; background: var(--paper-panel); border-radius: 5px; color: var(--ink-dim); font-size: 12px; line-height: 1.8; }
.wf-inherited { color: var(--ink-dim); font-size: 11px; }
.wf-finish { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--paper-line); }
.wf-finish label { display: grid; gap: 10px; font-size: 13px; max-width: 370px; }
.wf-finish :deep(select) { width: 100%; min-width: 0; padding: 9px 10px; border: 1px solid var(--paper-line); border-radius: 5px; background: var(--paper); color: var(--ink); font: inherit; }
.wf-finish > p { font-size: 11px; color: var(--ink-dim); line-height: 1.7; margin: 10px 0 0; }
.wf-name { display: grid; gap: 9px; flex: 1; min-width: 0; color: var(--ink-dim); font-size: 11px; }
.wf-name :deep(input) { width: 100%; min-width: 0; height: auto; border: 0; border-bottom: 1px solid var(--paper-line); border-radius: 0; padding: 8px 0; background: transparent; box-shadow: none; color: var(--ink); font: inherit; font-size: 20px; font-weight: 650; }
.wf-name :deep(input::placeholder) { color: var(--ink-dim); font-size: 16px; font-weight: normal; opacity: .75; }
.wf-more-button { display: grid; place-items: center; position: absolute; right: 6px; top: 8px; width: 28px; height: 28px; padding: 0; border: 0; border-radius: 4px; background: transparent; color: var(--ink-dim); font-size: 18px; line-height: 1; }
.wf-more-button:hover { background: var(--paper-panel); color: var(--ink); }
/* The top layer keeps the compact menu clear of the narrow-screen card scroller. */
.wf-preset-menu { position: fixed; inset: auto; margin: 0; width: 136px; padding: 4px; background: var(--paper-card); color: var(--ink); border: 1px solid var(--paper-line); border-radius: 6px; box-shadow: 0 4px 14px #0002; }
.wf-preset-menu button { display: block; width: 100%; background: none; color: var(--ink); border: 0; border-radius: 3px; padding: 8px 10px; text-align: left; font-size: 12px; }
.wf-preset-menu button:hover:not(:disabled) { background: var(--paper-panel); }
.wf-list-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.wf-list-heading h3 { margin: 0; font-size: 13px; }
.wf-list-heading h3 span { margin-left: 8px; color: var(--ink-dim); font-size: 11px; font-weight: normal; }
.wf-list-heading > small { color: var(--ink-dim); font-size: 11px; }
.wf-editor:has(.wf-empty) .wf-edit-fields { padding-block: 18px 10px; }
.wf-editor:has(.wf-empty) .wf-editor-head { padding-bottom: 14px; }
.wf-editor:has(.wf-empty) .wf-name :deep(input) { padding-block: 6px; }
.wf-editor:has(.wf-empty) .wf-toolbar { position: static; }
.wf-empty { text-align: center; padding: 16px 10px 18px; }
.wf-empty h3 { font-size: 16px; margin: 0 0 8px; }
.wf-empty p { color: var(--ink-dim); font-size: 12px; margin: 0 0 14px; line-height: 1.8; }
.wf-insert { display: flex; width: 100%; height: 22px; padding: 0; border: 0; align-items: center; justify-content: center; gap: 8px; background: transparent; color: var(--ink-dim); font-size: 10px; }
.wf-insert::before, .wf-insert::after { content: ''; height: 1px; background: var(--paper-line); flex: 1; opacity: .5; }
.wf-insert span { opacity: .65; }
.wf-insert:hover span, .wf-insert:focus-visible span { opacity: 1; color: var(--fox-gold); }
.wf-step { border: 0; border-top: 1px solid var(--paper-line); border-radius: 0; background: transparent; scroll-margin: 12px; }
.wf-step:last-of-type { border-bottom: 1px solid var(--paper-line); }
.wf-step.is-open { background: color-mix(in srgb, var(--paper-panel) 45%, transparent); box-shadow: inset 3px 0 var(--fox-gold); }
.wf-step-head { display: flex; align-items: center; padding: 0 9px 0 0; }
.wf-step-toggle { display: flex; align-items: center; gap: 13px; min-width: 0; flex: 1; border: 0; background: none; color: var(--ink); padding: 16px 12px; text-align: left; }
.wf-number { align-self: flex-start; margin-top: 1px; width: 28px; height: 28px; flex-shrink: 0; display: grid; place-items: center; color: var(--fox-gold); background: var(--paper-panel); border-radius: 4px; font-size: 11px; font-variant-numeric: tabular-nums; }
.wf-step-label { min-width: 0; flex: 1; }
.wf-step-label strong { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; font-size: 14px; line-height: 1.7; }
.wf-step-label strong small { font-weight: normal; color: var(--ink-dim); font-size: 10px; border: 1px solid var(--paper-line); padding: 0 5px; border-radius: 3px; }
.wf-step-label > span { display: block; color: var(--ink-dim); font-size: 11px; line-height: 1.7; margin-top: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wf-chevron { color: var(--ink-dim); font-size: 13px; }
.wf-step-tools { display: flex; border-left: 1px solid var(--paper-line); padding-left: 7px; }
.wf-step-tools button { border: 0; background: none; color: var(--ink-dim); width: 29px; height: 34px; border-radius: 4px; }
.wf-step-tools button:hover:not(:disabled) { background: var(--paper-panel); color: var(--ink); }
.wf-step-detail { padding: 0 18px 18px 53px; }
.wf-step-detail > p { font-size: 12px; color: var(--ink-dim); line-height: 1.7; margin: 0 0 14px; }
.wf-params { padding: 0; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.wf-error-policy { margin-top: 15px; padding-top: 14px; border-top: 1px dashed var(--paper-line); display: grid; gap: 10px; }
.wf-error-policy > span { color: var(--ink-dim); font-size: 11px; }
.wf-error-policy :deep(.segmented-control) { max-width: 390px; }
.wf-add { display: block; width: 100%; margin-top: 18px; border-style: dashed; background: transparent; color: var(--fox-gold); }
.wf-undo { display: flex; gap: 12px; align-items: center; font-size: 12px; margin-top: 14px; color: var(--ink-dim); }
.wf-undo button, .wf-text-button { background: none; border: 0; color: var(--fox-gold); font-size: 12px; padding: 6px 0; }
.wf-toolbar { display: flex; position: sticky; bottom: 0; z-index: 3; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; padding: 15px 26px; background: var(--paper-card); border-top: 1px solid var(--paper-line); border-radius: 0 0 10px 10px; box-shadow: 0 -6px 14px #49382104; }
.wf-editor:has(.wf-steps) .wf-toolbar { border-radius: 0; }
.wf-save-state { font-size: 11px; color: var(--ink-dim); }
.wf-save-state.unsaved { color: var(--fox-gold); }
.wf-actions { display: flex; gap: 9px; }
.wf-running { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; padding: 16px 26px; border-top: 1px solid var(--paper-line); font-size: 12px; }
.wf-message { margin: 0; padding: 12px 26px; font-size: 12px; }
.wf-danger { color: #a04b3a; }
button.wf-danger { color: #a04b3a; }
.wf-loading { padding: 30px clamp(22px, 4vw, 58px); color: var(--ink-dim); font-size: 13px; }
.wf-dialog { padding: 0; border: 1px solid var(--paper-line); border-radius: 12px; background: var(--paper-card); color: var(--ink); width: min(700px, calc(100% - 32px)); max-height: calc(100dvh - 48px); box-shadow: 0 22px 80px #251a1140; }
.wf-dialog::backdrop { background: #30291f80; }
.wf-picker[open] { display: flex; flex-direction: column; }
.wf-dialog-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding: 25px 26px 18px; }
.wf-dialog h2 { font-size: 20px; margin: 0; }
.wf-dialog-head p { font-size: 12px; color: var(--ink-dim); margin: 9px 0 0; }
.wf-close { color: var(--ink-dim); font-size: 25px; border: 0; background: none; padding: 0 4px; }
.wf-search-area { padding: 0 26px 17px; border-bottom: 1px solid var(--paper-line); }
.wf-search { display: flex; align-items: center; gap: 10px; padding: 0 12px; border: 1px solid var(--paper-line); background: var(--paper); border-radius: var(--r-sm); }
.wf-search > span { color: var(--ink-dim); font-size: 25px; }
.wf-search input { width: 100%; min-width: 0; background: none; border: 0; padding: 12px 0; color: var(--ink); font: inherit; font-size: 13px; }
.wf-categories { display: flex; gap: 6px; margin-top: 14px; }
.wf-categories button { background: transparent; color: var(--ink-dim); border: 1px solid transparent; padding: 6px 13px; font-size: 12px; border-radius: 5px; }
.wf-categories button[aria-pressed='true'] { background: var(--paper-panel); border-color: var(--paper-line); color: var(--ink); }
.wf-catalog { overflow-y: auto; min-height: 0; padding: 2px 26px 24px; overscroll-behavior: contain; }
.wf-catalog h3 { margin: 21px 0 10px; font-size: 12px; color: var(--ink-dim); font-weight: 500; }
.wf-catalog-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
.wf-catalog-grid > button { display: flex; align-items: center; justify-content: space-between; gap: 12px; text-align: left; padding: 14px; color: var(--ink); border: 1px solid var(--paper-line); border-radius: 7px; background: var(--paper-card); min-width: 0; }
.wf-catalog-grid > button:hover:not(:disabled) { border-color: var(--fox-gold); background: var(--paper-panel); }
.wf-catalog-grid strong { display: block; font-size: 13px; font-weight: 600; }
.wf-catalog-grid small { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; margin-top: 7px; font-size: 11px; color: var(--ink-dim); line-height: 1.6; }
.wf-catalog-plus { flex-shrink: 0; color: var(--fox-gold); font-size: 18px; }
.wf-no-results { padding: 30px 0; text-align: center; font-size: 13px; color: var(--ink-dim); }
.wf-picker-footer { flex-shrink: 0; border-top: 1px solid var(--paper-line); padding: 16px 26px; display: flex; align-items: center; justify-content: space-between; gap: 18px; }
.wf-picker-footer > span { font-size: 12px; color: var(--ink); line-height: 1.6; }
.wf-picker-footer small { display: block; color: var(--ink-dim); font-size: 11px; margin-top: 3px; }
.wf-picker-footer button { flex-shrink: 0; }
.wf-switch { width: min(480px, calc(100% - 32px)); padding: 26px; }
.wf-switch > p { color: var(--ink-dim); font-size: 13px; line-height: 1.7; }
.wf-switch-actions { display: flex; justify-content: flex-end; gap: 10px; flex-wrap: wrap; margin-top: 24px; }
@media (max-width: 1000px) {
  .wf-layout { grid-template-columns: 190px minmax(0, 1fr); }
  .wf-edit-fields { padding: 20px 18px; }
  .wf-toolbar { padding: 14px 18px; }
  .wf-step-detail { padding-left: 18px; }
}
@media (max-width: 720px) {
  .wf-layout { grid-template-columns: 1fr; }
  .wf-library { padding: 14px; border-right: 0; border-bottom: 1px solid var(--line); display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
  .wf-editor { margin: 16px 14px; box-shadow: 3px 3px 0 #cfc1aa; }
  .wf-library > header { flex: 1; margin: 0; }
  .wf-new { width: auto; }
  .wf-presets { display: flex; width: 100%; overflow-x: auto; gap: 8px; margin: 0; padding-bottom: 3px; }
  .wf-preset { flex: 0 0 auto; max-width: 210px; border-color: var(--paper-line); }
  .wf-preset-select { padding: 9px 38px 9px 13px; }
  .wf-library-note { display: none; }
  .wf-editor-head { padding-bottom: 20px; gap: 8px; }
  .wf-name :deep(input) { font-size: 18px; }
  .wf-name :deep(input::placeholder) { font-size: 13px; }
  .wf-edit-fields { padding: 18px 12px; }
  .wf-empty { padding: 26px 0; }
  .wf-empty h3 { font-size: 15px; }
  .wf-list-heading > small { font-size: 10px; }
  .wf-step-head { flex-wrap: wrap; padding: 0; }
  .wf-step-toggle { padding: 12px 10px; gap: 9px; width: 100%; flex-basis: 100%; }
  .wf-number { width: 23px; height: 25px; }
  .wf-step-tools { margin: 0 8px 6px auto; border: 0; gap: 4px; }
  .wf-step-tools button { width: 36px; height: 32px; background: var(--paper-panel); }
  .wf-step-detail { padding: 8px 12px 15px; }
  .wf-params { grid-template-columns: 1fr; }
  .wf-toolbar { padding: 12px; gap: 8px; }
  .wf-save-state { width: 100%; }
  .wf-actions { width: 100%; }
  .wf-actions button { flex: 1; padding-inline: 8px; }
  .wf-running, .wf-message { padding: 12px; }
  .wf-dialog { max-height: calc(100dvh - 24px); width: calc(100% - 20px); border-radius: 9px; }
  .wf-dialog-head { padding: 20px 16px 16px; }
  .wf-search-area { padding: 0 16px 14px; }
  .wf-categories { gap: 3px; justify-content: space-between; }
  .wf-categories button { padding: 6px 10px; }
  .wf-catalog { padding: 0 16px 18px; }
  .wf-catalog-grid { grid-template-columns: 1fr; }
  .wf-picker-footer { padding: 12px 16px; gap: 10px; }
  .wf-picker-footer > span { font-size: 11px; }
  .wf-switch { padding: 22px 18px; }
}
/* 像素主题：侧栏与 .system-nav 同板。 */
:global(body[data-theme='pixel']) .wf-library { background: #e8dfca; border-color: #c8bea5; }
</style>
