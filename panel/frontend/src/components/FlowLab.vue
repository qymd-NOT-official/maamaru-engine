<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import PanelHeader from './PanelHeader.vue'
import PaperCard from './PaperCard.vue'
import PixelControl from './PixelControl.vue'
import ParamField from './ParamField.vue'
import type { FlowBuiltinDef, FlowLabFlow, FlowStep, FlowStepCategory, FlowStepDef, FlowTestResult, ParamField as Field, TemplateLabFrame, TemplateLabRoi, TemplateLabSession } from '../types'
import { matchesRule } from '../visibility'
import { applyResize, clampRect, HANDLE_CURSORS, HANDLE_SCREEN_PX, handlePoints, hitHandle } from './templateLabSelection'
import type { HandleId, SelRect } from './templateLabSelection'

const flows = ref<FlowLabFlow[]>([])
const warnings = ref<string[]>([])
const defs = ref<FlowStepDef[]>([])
const builtins = ref<FlowBuiltinDef[]>([])
const templates = ref<string[]>([])
const savedRois = ref<TemplateLabRoi[]>([])
const draft = ref<FlowLabFlow | null>(null)
const expanded = ref<FlowStep | null>(null)
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)
const starting = ref(false)
const message = ref('')
const failed = ref(false)
const picker = ref<HTMLDialogElement | null>(null)
const switchDialog = ref<HTMLDialogElement | null>(null)
const moreMenu = ref<HTMLElement | null>(null)
const menuFlow = ref<FlowLabFlow | null>(null)
const menuPosition = ref({ left: '0px', top: '0px' })
const search = ref('')
const searchInput = ref<HTMLInputElement | null>(null)
const insertAt = ref(0)
const lastAdded = ref('')
const probe = ref<FlowTestResult | null>(null)
const probingStep = ref<FlowStep | null>(null)
let pendingSwitch: (() => void) | null = null

const maxSteps = 50 // touken/flow_engine.py: MAX_STEPS
const categoryOrder: FlowStepCategory[] = ['认', '点', '结构']
const categoryBadge: Record<FlowStepCategory, string> = { 认: 'see', 点: 'tap', 结构: 'ctrl' }
const categoryHint: Record<FlowStepCategory, string> = { 认: '认：先看画面，不伸手', 点: '点：认到才动手', 结构: '结构：管路线和内置积木' }

const locked = computed(() => saving.value || starting.value)
// 官方流程 = 随包发布的样板：展品模式，只读可跑可复制，不许改
const isOfficial = computed(() => draft.value?.official === true)
const officialFlows = computed(() => flows.value.filter(flow => flow.official))
const myFlows = computed(() => flows.value.filter(flow => !flow.official))
const dirty = computed(() => {
  if (!draft.value) return false
  const saved = flows.value.find(flow => flow.id === draft.value?.id)
  return !saved || saved.name !== draft.value.name || canonical(saved.steps) !== canonical(draft.value.steps)
})
const valid = computed(() => !!draft.value?.name.trim() && !!draft.value?.steps.length)
const groups = computed(() => categoryOrder
  .map(category => ({ category, steps: defs.value.filter(def => def.category === category && `${def.label} ${def.desc}`.toLowerCase().includes(search.value.trim().toLowerCase())) }))
  .filter(group => group.steps.length))

function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T }
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}
function defOf(type: string) { return defs.value.find(def => def.type === type) }
function builtinOf(name: unknown) { return builtins.value.find(item => item.name === name) }
function stepLabel(step: FlowStep) { return step.label || defOf(step.type)?.label || step.type }
function valueFor(step: FlowStep, key: string) {
  const def = defOf(step.type)
  return step.params[key] ?? def?.params.find(field => field.key === key)?.default ?? ''
}
function isVisible(field: Field, step: FlowStep) {
  return matchesRule(field.visibleWhen, key => valueFor(step, key))
}
function visibleFields(step: FlowStep) {
  return (defOf(step.type)?.params || []).filter(field => isVisible(field, step))
}
// click_point 的 point 由下方专属「命名点」区块渲染（带互斥提示），通用字段区跳过它，
// 同个参数只留一个输入口
function editorFields(step: FlowStep) {
  return visibleFields(step).filter(field => step.type !== 'click_point' || field.key !== 'point')
}
function summary(step: FlowStep) {
  if (step.type === 'click_point' && hasNamedPoint(step)) {
    return `命名点：${String(step.params.point).trim()}`
  }
  return editorFields(step).filter(field => field.type !== 'note').slice(0, 3).map(field => {
    const value = valueFor(step, field.key)
    const option = field.options?.find(option => String(Array.isArray(option) ? option[0] : option) === String(value))
    const label = Array.isArray(option) ? option[1] : option
    const display = label ?? (Array.isArray(value) ? `[${value.join(', ')}]` : typeof value === 'boolean' ? (value ? '开启' : '关闭') : value)
    return `${field.label?.split(/[（(]/)[0]}：${display === '' || display == null ? '未设置' : display}`
  }).join(' · ') || defOf(step.type)?.desc || '点开设置'
}
function tell(text: string, error = false) { message.value = text; failed.value = error }
function fail(e: unknown) { tell(e instanceof Error && e.message ? e.message : '操作失败，请检查后端连接', true) }
function closeMenu() { moreMenu.value?.hidePopover() }

// ---- 流程选择器与操作（编辑器顶部的下拉 + 更多菜单）----
const flowPick = ref('')
// 下拉跟着草稿走：新建/未保存时回到占位项
watch(() => draft.value?.id, (id) => { flowPick.value = id || '' })
// 不能用 @change 联动：PixelControl 的 change 先于 v-model 回写触发，会拿到旧值
watch(flowPick, (id) => {
  if (!id) { if (draft.value?.id) newFlow(); return }
  const flow = flows.value.find(item => item.id === id)
  if (flow) select(flow)
})
async function openFlowMenu(event: MouseEvent) {
  const current = flows.value.find(item => item.id === draft.value?.id)
  if (!current) return
  const anchor = (event.currentTarget as HTMLElement).getBoundingClientRect()
  menuFlow.value = current
  await nextTick()
  moreMenu.value?.showPopover()
  const height = moreMenu.value?.offsetHeight || 96
  menuPosition.value = {
    left: `${Math.max(8, Math.min(anchor.left, window.innerWidth - 144))}px`,
    top: `${anchor.bottom + height + 8 <= window.innerHeight ? anchor.bottom + 4 : Math.max(8, anchor.top - height - 4)}px`,
  }
}
async function copyCurrent() {
  closeMenu()
  const current = flows.value.find(item => item.id === draft.value?.id)
  if (!current) return
  saving.value = true
  try {
    const result = await api.flowLabCopyFlow(current.id)
    await load(true)
    setDraft(result.flow)
    tell(`已复制成「${result.flow.name}」，随便改`)
  } catch (e) { fail(e) } finally { saving.value = false }
}
async function renameFlow() {
  const current = menuFlow.value
  closeMenu()
  if (!current || !draft.value) return
  const name = window.prompt('给流程改个名字', draft.value.name)
  if (name === null) return
  const trimmed = name.trim().slice(0, 30)
  if (!trimmed) { tell('流程名字不能为空', true); return }
  draft.value.name = trimmed
  if (draft.value.id === current.id && !dirty.value) await save()
  else tell('改名会跟着下次保存落盘')
}
function setDraft(flow: FlowLabFlow) {
  draft.value = clone(flow)
  expanded.value = null
  probe.value = null
  message.value = ''
  closeMenu()
}
function switchTo(action: () => void) {
  if (locked.value) return
  if (dirty.value && (draft.value?.name.trim() || draft.value?.steps.length)) {
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
function newFlow() { switchTo(() => setDraft({ id: '', name: '', steps: [] })) }
function select(flow: FlowLabFlow) {
  if (draft.value?.id === flow.id) return
  switchTo(() => setDraft(flow))
}
async function duplicate(flow: FlowLabFlow) {
  closeMenu()
  saving.value = true
  try {
    const result = await api.flowLabDuplicateFlow(flow.id)
    await load(true)
    setDraft(result.flow)
    tell(`已复制出「${result.flow.name}」，改改就能用`)
  } catch (e) { fail(e) } finally { saving.value = false }
}
async function remove(flow: FlowLabFlow) {
  closeMenu()
  if (locked.value) return
  if (!window.confirm(`删除「${flow.name}」？这条已保存的流程将无法恢复。`)) return
  saving.value = true
  try {
    await api.flowLabDeleteFlow(flow.id)
    flows.value = flows.value.filter(item => item.id !== flow.id)
    if (draft.value?.id === flow.id) setDraft(flows.value[0] || { id: '', name: '', steps: [] })
    tell(`已删除「${flow.name}」`)
  } catch (e) { fail(e) } finally { saving.value = false }
}

async function load(keepDraft = false) {
  loading.value = true
  loadError.value = ''
  try {
    const [flowData, stepData, tplData, roiData, sessionData] = await Promise.all([
      api.flowLabFlows(), api.flowLabSteps(), api.flowLabTemplates(), api.flowLabRois(), api.templateLabSessions(),
    ])
    flows.value = flowData.flows || []
    warnings.value = flowData.warnings || []
    defs.value = stepData.steps || []
    builtins.value = stepData.builtins || []
    templates.value = tplData.templates || []
    savedRois.value = roiData.rois || []
    sessions.value = sessionData.sessions || []
    if (!keepDraft || !draft.value) setDraft(flows.value[0] || { id: '', name: '', steps: [] })
    else if (draft.value.id) {
      const current = flows.value.find(flow => flow.id === draft.value?.id)
      if (current && canonical(current) !== canonical(draft.value)) setDraft(current)
    }
    if (!currentSession.value || !sessions.value.some(s => s.id === currentSession.value)) {
      if (sessions.value.length) applySession(sessions.value[0])
    }
  } catch (e) {
    loadError.value = e instanceof Error ? `加载失败：${e.message}` : '加载失败，请重试'
  } finally { loading.value = false }
}

// ---- 步骤编辑 ----
let idCounter = 0
function uniqueStepId() {
  let id = ''
  do { id = `s${Date.now().toString(36)}${(idCounter++).toString(36)}` }
  while (draft.value?.steps.some(step => step.id === id))
  return id
}
function openPicker(index: number) {
  insertAt.value = index
  search.value = ''
  lastAdded.value = ''
  picker.value?.showModal()
  nextTick(() => searchInput.value?.focus())
}
function addStep(def: FlowStepDef) {
  if (!draft.value || draft.value.steps.length >= maxSteps) return
  const params: Record<string, unknown> = {}
  for (const field of def.params) {
    if (field.type === 'note' || field.default == null) continue
    params[field.key] = clone(field.default)
  }
  // 不写 on_fail：翻车处置由引擎统一管（记一笔，能安全回本丸才继续）
  const step: FlowStep = { id: uniqueStepId(), type: def.type, params }
  draft.value.steps.splice(insertAt.value++, 0, step)
  expanded.value = draft.value.steps[insertAt.value - 1]
  lastAdded.value = `已添加「${def.label}」，可以继续选`
}
function closePicker() {
  picker.value?.close()
  nextTick(() => {
    if (expanded.value) document.getElementById(`fl-step-${expanded.value.id}`)?.scrollIntoView({ block: 'nearest' })
  })
}
function moveStep(index: number, delta: number) {
  if (!draft.value) return
  const steps = draft.value.steps
  const target = index + delta
  if (target < 0 || target >= steps.length) return
  const [step] = steps.splice(index, 1)
  steps.splice(target, 0, step)
  tell(`「${stepLabel(step)}」已移到第 ${target + 1} 步`)
}
function removeStep(index: number) {
  if (!draft.value) return
  const [step] = draft.value.steps.splice(index, 1)
  if (expanded.value === step) expanded.value = null
  tell(`已移除「${stepLabel(step)}」`)
}
function useRoiFromStore(step: FlowStep, field: Field, name: string) {
  const roi = savedRois.value.find(item => item.name === name)
  if (!roi) return
  step.params[field.key] = [roi.x, roi.y, roi.x + roi.w, roi.y + roi.h]
  tell(`已引用 ROI「${name}」`)
}
function clearRoi(step: FlowStep, field: Field) {
  delete step.params[field.key]
  if (expanded.value === step) selection.value = null
}

// ---- 逻辑引用（@ 开头，素材包解析）：模板/ROI/命名点 ----
// 原始值（模板路径、xyxy、x/y）照旧可用，与 @ 引用二选一填。
function templatePreview(step: FlowStep, field: Field) {
  const value = String(valueFor(step, field.key) ?? '').trim()
  return value && !value.startsWith('@') ? value : ''
}
function roiValue(step: FlowStep, field: Field): number[] | string {
  const value = valueFor(step, field.key)
  if (Array.isArray(value)) return value as number[]
  return typeof value === 'string' ? value : ''
}
function roiDisplay(step: FlowStep, field: Field) {
  const value = roiValue(step, field)
  if (Array.isArray(value)) return `[${value.join(', ')}]`
  return value || '未框选（留空 = 全屏）'
}
function setRoiRef(step: FlowStep, field: Field, value: unknown) {
  const text = String(value ?? '').trim()
  if (text) step.params[field.key] = text
  else if (typeof step.params[field.key] === 'string') delete step.params[field.key]
}
function setNamedPoint(step: FlowStep, value: unknown) {
  const name = String(value ?? '').trim()
  if (name) step.params.point = name
  else delete step.params.point
}
function hasNamedPoint(step: FlowStep) {
  return !!String(step.params.point ?? '').trim()
}

async function save(): Promise<boolean> {
  if (!draft.value || saving.value || !valid.value) return false
  saving.value = true
  const snapshot = clone(draft.value)
  snapshot.name = snapshot.name.trim()
  for (const step of snapshot.steps) if (!step.label) delete step.label
  try {
    if (snapshot.id) {
      const result = await api.flowLabUpdateFlow(snapshot)
      Object.assign(snapshot, result.flow)
    } else {
      const result = await api.flowLabCreateFlow({ name: snapshot.name, steps: snapshot.steps })
      Object.assign(snapshot, result.flow)
    }
    const index = flows.value.findIndex(item => item.id === snapshot.id)
    if (index >= 0) flows.value[index] = clone(snapshot)
    else flows.value.push(clone(snapshot))
    draft.value.id = snapshot.id
    tell('流程已保存')
    return true
  } catch (e) {
    fail(e)
    return false
  } finally { saving.value = false }
}
async function run() {
  if (locked.value || !valid.value) return
  starting.value = true
  try {
    if (dirty.value && !await save()) return
    const result = await api.run('custom_flow', { flow_id: draft.value!.id })
    if (!result.ok) throw new Error('没有启动成功——可能有别的任务正在跑')
    tell(`「${draft.value!.name}」已开跑，去功能的执务台看日志 →`)
  } catch (e) { fail(e) } finally { starting.value = false }
}

// ---- 单步试跑 ----
async function testStep(step: FlowStep) {
  probingStep.value = step
  probe.value = null
  tell(`正在试跑「${stepLabel(step)}」……`)
  try {
    probe.value = await api.flowLabTestStep(clone(step))
    tell(probe.value.kind === 'recognize'
      ? (probe.value.hit ? `认到了${probe.value.score != null ? `，分数 ${probe.value.score}` : ''}` : '没认到——调调阈值或框准一点')
      : '预览出来了（不会真点下去）')
  } catch (e) { fail(e) } finally { probingStep.value = null }
}

// ---- 画布（帧来自模板工坊会话，几何库复用 templateLabSelection）----
const sessions = ref<TemplateLabSession[]>([])
const currentSession = ref('')
const frames = ref<TemplateLabFrame[]>([])
const frameIdx = ref(0)
const historyPick = ref('')
const zoom = ref(1)
const selection = ref<SelRect | null>(null)
const dragging = ref(false)
const pointPicking = ref(false)
const pointRole = ref<'start' | 'end'>('start')
const capturing = ref(false)
const canvasRef = ref<HTMLCanvasElement | null>(null)
const scrollRef = ref<HTMLElement | null>(null)
let dragStart: { x: number; y: number } | null = null
let resizeHandle: HandleId | null = null
let resizeBase: SelRect | null = null
let lastPointer: { x: number; y: number } | null = null
let scrollRaf = 0
let renderToken = 0

const frameMeta = computed(() => frames.value[frameIdx.value] ?? null)
// 100% = 恰好填满右栏可用宽度；200%/400% 在画布容器内滚动。
// 画布 CSS 宽用百分比，坐标换算靠 canvas 位图宽 / 实际渲染宽（currentScale），
// 任何缩放下拖框、八向手柄、命中标记都不会错位。
const canvasStyle = computed(() => {
  const meta = frameMeta.value
  if (!meta) return { width: '0px', height: '0px' }
  return { width: `${zoom.value * 100}%`, height: 'auto', display: 'block' }
})
// 当前图像像素 / CSS 像素 的换算比：缩放级别、右栏宽度变化都从这里实时读，
// 不缓存、不看 zoom 值本身——它就是此刻画布的真实映射。
function currentScale() {
  const canvas = canvasRef.value
  if (!canvas) return 1
  const rect = canvas.getBoundingClientRect()
  return rect.width > 0 ? canvas.width / rect.width : 1
}
const selText = computed(() => {
  const sel = selection.value
  return sel && sel.w > 0 && sel.h > 0 ? `x ${sel.x}，y ${sel.y}，宽 ${sel.w}，高 ${sel.h}` : '按住鼠标拖一个框'
})
const probePoint = computed(() => probe.value?.point || null)
const probeLine = computed(() => (probe.value?.from && probe.value?.to ? { from: probe.value.from, to: probe.value.to } : null))

function applySession(session: TemplateLabSession) {
  currentSession.value = session.id
  frames.value = session.frames
  frameIdx.value = 0
  selection.value = null
  probe.value = null
}
watch(historyPick, (id) => {
  const picked = sessions.value.find(s => s.id === id)
  if (picked && picked.id !== currentSession.value) applySession(picked)
})
watch(frameIdx, () => { if (!dragging.value) renderCanvas() })
watch(zoom, () => { renderCanvas() })
watch(selection, () => { renderCanvas() })
watch(probe, () => { renderCanvas() })
// 展开步骤时，把它已有的 ROI 画回画布；收起则清掉
watch(expanded, (step) => {
  pointPicking.value = false
  const field = step ? visibleFields(step).find(item => item.type === 'roi') : null
  const roi = field && step ? (step.params[field.key] as number[] | undefined) : null
  selection.value = Array.isArray(roi) && roi.length === 4
    ? clampRect({ x: roi[0], y: roi[1], w: roi[2] - roi[0], h: roi[3] - roi[1] }, 1280, 720)
    : null
})

async function captureOne() {
  capturing.value = true
  try {
    const result = await api.templateLabCapture(1, 300, '流程工坊取帧')
    await loadSessions()
    const session = sessions.value.find(s => s.id === result.session)
    if (session) {
      historyPick.value = session.id
      applySession(session)
    }
    tell('抓了一帧新的')
  } catch (e) { fail(e) } finally { capturing.value = false }
}
async function loadSessions() {
  try {
    const data = await api.templateLabSessions()
    sessions.value = data.sessions || []
  } catch { /* 会话列表失败不挡编辑器 */ }
}

const frameUrl = (session: string, idx: number) => api.templateLabFrameUrl(session, idx)
const imageCache = new Map<string, Promise<HTMLImageElement>>()
function loadFrameImage(session: string, idx: number): Promise<HTMLImageElement> {
  const key = `${session}:${idx}`
  let pending = imageCache.get(key)
  if (!pending) {
    pending = new Promise((resolve, reject) => {
      const img = new Image()
      img.onload = () => resolve(img)
      img.onerror = () => { imageCache.delete(key); reject(new Error('帧加载失败')) }
      img.src = frameUrl(session, idx)
    })
    imageCache.set(key, pending)
  }
  return pending
}

function toImageCoords(clientX: number, clientY: number) {
  const canvas = canvasRef.value
  if (!canvas || !canvas.width) return { x: 0, y: 0 }
  const rect = canvas.getBoundingClientRect()
  return {
    x: (clientX - rect.left) * (canvas.width / rect.width),
    y: (clientY - rect.top) * (canvas.height / rect.height),
  }
}

// 画布上点一下回填坐标：盲点填 x/y，滑动填起点或终点
function applyPointPick(p: { x: number; y: number }) {
  const step = expanded.value
  if (!step || !pointPicking.value) return
  const x = Math.round(p.x)
  const y = Math.round(p.y)
  if (step.type === 'click_point') {
    step.params.x = x
    step.params.y = y
    tell(`已回填点击坐标 (${x}, ${y})`)
  } else if (step.type === 'swipe') {
    if (pointRole.value === 'start') {
      step.params.x1 = x
      step.params.y1 = y
      tell(`已回填滑动起点 (${x}, ${y})，再点一下终点`)
      pointRole.value = 'end'
    } else {
      step.params.x2 = x
      step.params.y2 = y
      tell(`已回填滑动终点 (${x}, ${y})`)
    }
  }
}

function startPointPicking(role: 'single' | 'start' | 'end') {
  if (!expanded.value) return
  pointRole.value = role === 'end' ? 'end' : 'start'
  pointPicking.value = true
  tell(role === 'single' ? '在右侧画布上点一下要点的位置' : role === 'start' ? '在右侧画布上点滑动的起点' : '在右侧画布上点滑动的终点')
}

function currentRoiField(step: FlowStep | null): Field | null {
  return step ? visibleFields(step).find(item => item.type === 'roi') || null : null
}

async function renderCanvas() {
  const canvas = canvasRef.value
  if (!canvas) return
  const token = ++renderToken
  const meta = frameMeta.value
  if (!currentSession.value || !meta) {
    canvas.width = 0
    canvas.height = 0
    return
  }
  try {
    const img = await loadFrameImage(currentSession.value, meta.idx)
    if (token !== renderToken) return
    if (canvas.width !== img.naturalWidth) canvas.width = img.naturalWidth
    if (canvas.height !== img.naturalHeight) canvas.height = img.naturalHeight
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.imageSmoothingEnabled = false
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(img, 0, 0)
    // 试跑标记：认到的点 / 将要点的位置 / 滑动线
    const point = probePoint.value
    if (point) {
      ctx.strokeStyle = point && probe.value?.hit !== false ? '#3f7d4e' : '#a04b3a'
      ctx.lineWidth = 2
      ctx.beginPath()
      ctx.moveTo(point[0] - 10, point[1]); ctx.lineTo(point[0] + 10, point[1])
      ctx.moveTo(point[0], point[1] - 10); ctx.lineTo(point[0], point[1] + 10)
      ctx.stroke()
      ctx.beginPath()
      ctx.arc(point[0], point[1], 6, 0, Math.PI * 2)
      ctx.stroke()
    }
    const line = probeLine.value
    if (line) {
      ctx.strokeStyle = '#b3781f'
      ctx.lineWidth = 2
      ctx.setLineDash([6, 4])
      ctx.beginPath()
      ctx.moveTo(line.from[0], line.from[1])
      ctx.lineTo(line.to[0], line.to[1])
      ctx.stroke()
      ctx.setLineDash([])
    }
    // 框选（ROI）：金框 + 八向手柄，与模板工坊同款。
    // 手柄屏幕尺寸恒定，要按当前真实缩放（位图宽/渲染宽）换算回图像坐标。
    const scale = currentScale()
    const sel = selection.value
    if (sel && sel.w > 0 && sel.h > 0) {
      ctx.fillStyle = 'rgba(212, 160, 23, 0.12)'
      ctx.fillRect(sel.x, sel.y, sel.w, sel.h)
      ctx.strokeStyle = '#d4a017'
      ctx.lineWidth = Math.max(1, 1.5)
      ctx.strokeRect(sel.x + 0.5, sel.y + 0.5, sel.w - 1, sel.h - 1)
      const hs = HANDLE_SCREEN_PX / scale
      ctx.fillStyle = '#d4a017'
      ctx.strokeStyle = '#fffaf0'
      ctx.lineWidth = Math.max(1 / scale, 0.5)
      for (const p of Object.values(handlePoints(sel))) {
        ctx.fillRect(p.x - hs / 2, p.y - hs / 2, hs, hs)
        ctx.strokeRect(p.x - hs / 2, p.y - hs / 2, hs, hs)
      }
    }
  } catch { /* 帧没加载出来就留空画布 */ }
}

function applyDrag() {
  if (!dragging.value || !lastPointer) return
  const p = toImageCoords(lastPointer.x, lastPointer.y)
  if (resizeHandle && resizeBase) {
    const meta = frameMeta.value
    selection.value = applyResize(resizeBase, resizeHandle, p, meta ? { width: meta.width, height: meta.height } : undefined)
  } else if (dragStart) {
    selection.value = clampRect({
      x: Math.min(dragStart.x, p.x),
      y: Math.min(dragStart.y, p.y),
      w: Math.abs(p.x - dragStart.x),
      h: Math.abs(p.y - dragStart.y),
    }, frameMeta.value?.width || 1280, frameMeta.value?.height || 720)
  }
}

const SCROLL_EDGE_PX = 24
function autoScrollTick() {
  const el = scrollRef.value
  if (el && lastPointer) {
    const r = el.getBoundingClientRect()
    let dx = 0
    let dy = 0
    if (lastPointer.x < r.left + SCROLL_EDGE_PX) dx = lastPointer.x - (r.left + SCROLL_EDGE_PX)
    else if (lastPointer.x > r.right - SCROLL_EDGE_PX) dx = lastPointer.x - (r.right - SCROLL_EDGE_PX)
    if (lastPointer.y < r.top + SCROLL_EDGE_PX) dy = lastPointer.y - (r.top + SCROLL_EDGE_PX)
    else if (lastPointer.y > r.bottom - SCROLL_EDGE_PX) dy = lastPointer.y - (r.bottom - SCROLL_EDGE_PX)
    dx = Math.max(-SCROLL_EDGE_PX, Math.min(SCROLL_EDGE_PX, dx))
    dy = Math.max(-SCROLL_EDGE_PX, Math.min(SCROLL_EDGE_PX, dy))
    if (dx || dy) {
      el.scrollLeft += dx
      el.scrollTop += dy
      applyDrag()
    }
  }
  scrollRaf = window.requestAnimationFrame(autoScrollTick)
}
function onWindowMouseMove(e: MouseEvent) {
  lastPointer = { x: e.clientX, y: e.clientY }
  applyDrag()
}
function onWindowMouseUp(e: MouseEvent) {
  if (!dragging.value) return
  lastPointer = { x: e.clientX, y: e.clientY }
  applyDrag()
  stopWindowDrag()
  // 拖完把框回填给展开步骤的 ROI 参数
  const step = expanded.value
  const field = currentRoiField(step)
  const sel = selection.value
  if (field && step && sel && sel.w >= 2 && sel.h >= 2) {
    step.params[field.key] = [sel.x, sel.y, sel.x + sel.w, sel.y + sel.h]
    tell(`已回填 ROI [${sel.x}, ${sel.y}, ${sel.x + sel.w}, ${sel.y + sel.h}]`)
  }
}
function stopWindowDrag() {
  window.removeEventListener('mousemove', onWindowMouseMove)
  window.removeEventListener('mouseup', onWindowMouseUp)
  if (scrollRaf) window.cancelAnimationFrame(scrollRaf)
  scrollRaf = 0
  dragging.value = false
  dragStart = null
  resizeHandle = null
  resizeBase = null
  lastPointer = null
}
function onMouseDown(e: MouseEvent) {
  if (!frameMeta.value) return
  if (pointPicking.value) {
    applyPointPick(toImageCoords(e.clientX, e.clientY))
    return
  }
  if (dragging.value) stopWindowDrag()
  const p = toImageCoords(e.clientX, e.clientY)
  const sel = selection.value
  const handle = sel && sel.w > 0 && sel.h > 0 ? hitHandle(sel, p, currentScale()) : null
  if (handle && sel) {
    resizeHandle = handle
    resizeBase = { ...sel }
    dragStart = null
  } else {
    resizeHandle = null
    resizeBase = null
    dragStart = p
    selection.value = { x: Math.round(p.x), y: Math.round(p.y), w: 0, h: 0 }
  }
  dragging.value = true
  lastPointer = { x: e.clientX, y: e.clientY }
  window.addEventListener('mousemove', onWindowMouseMove)
  window.addEventListener('mouseup', onWindowMouseUp)
  scrollRaf = window.requestAnimationFrame(autoScrollTick)
}
function onCanvasHover(e: MouseEvent) {
  const canvas = canvasRef.value
  if (!canvas) return
  if (pointPicking.value) {
    canvas.style.cursor = 'copy'
    return
  }
  if (dragging.value) return
  const sel = selection.value
  const handle = sel && sel.w > 0 && sel.h > 0
    ? hitHandle(sel, toImageCoords(e.clientX, e.clientY), currentScale())
    : null
  canvas.style.cursor = handle ? HANDLE_CURSORS[handle] : 'crosshair'
}
function nudgeSelection(dx: number, dy: number) {
  const sel = selection.value
  if (!sel) return
  selection.value = clampRect({ ...sel, x: sel.x + dx, y: sel.y + dy }, frameMeta.value?.width || 1280, frameMeta.value?.height || 720)
}
function onKeyDown(e: KeyboardEvent) {
  const target = e.target as HTMLElement | null
  if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable)) return
  if (e.key === 'Escape' && pointPicking.value) {
    pointPicking.value = false
    return
  }
  if (!selection.value) return
  const step = e.shiftKey ? 10 : 1
  const dirs: Record<string, [number, number]> = {
    ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step],
  }
  const dir = dirs[e.key]
  if (dir) {
    e.preventDefault()
    nudgeSelection(dir[0], dir[1])
  }
}
function protectDraft(event: BeforeUnloadEvent) {
  if (dirty.value && (draft.value?.name.trim() || draft.value?.steps.length)) {
    event.preventDefault()
    event.returnValue = ''
  }
}
// 画布要等 loading 完才挂载（v-else 渲染），用 watch 挂 ResizeObserver：
// 右栏宽度变化（窗口缩放、断点切换）→ 重新按新宽度适配绘制。
let canvasObserver: ResizeObserver | null = null
watch(canvasRef, (el) => {
  canvasObserver?.disconnect()
  canvasObserver = null
  if (el && typeof ResizeObserver !== 'undefined') {
    canvasObserver = new ResizeObserver(() => { renderCanvas() })
    canvasObserver.observe(el)
  }
})
onMounted(() => {
  load()
  window.addEventListener('keydown', onKeyDown)
  window.addEventListener('beforeunload', protectDraft)
  window.addEventListener('scroll', closeMenu, true)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeyDown)
  window.removeEventListener('beforeunload', protectDraft)
  window.removeEventListener('scroll', closeMenu, true)
  stopWindowDrag()
  canvasObserver?.disconnect()
})
</script>

<template>
  <section class="flow-lab">
    <PanelHeader variant="page" title="流程工坊" subtitle="认得出、点得准的自动化流程，自己拼，不用等发版。" />
    <p v-if="loading" class="fl-loading">正在取出流程和素材…</p>
    <p v-else-if="loadError" class="fl-loading" role="alert">{{ loadError }} <button class="fl-button" @click="load()">重新加载</button></p>
    <div v-else class="fl-layout">
      <PaperCard v-if="draft" variant="settings" class="fl-editor">
        <fieldset :disabled="locked" class="fl-edit-fields">
          <div class="fl-flowbar">
            <label class="fl-flowpick">流程
              <PixelControl v-model="flowPick" as="select">
                <option value="" disabled>{{ flows.length ? '选一条流程' : '还没有流程，先建一条' }}</option>
                <optgroup v-if="officialFlows.length" label="官方流程（只读样板）">
                  <option v-for="flow in officialFlows" :key="flow.id" :value="flow.id">「官方」{{ flow.name }}（{{ flow.steps.length }} 步）</option>
                </optgroup>
                <optgroup v-if="myFlows.length" label="我的流程">
                  <option v-for="flow in myFlows" :key="flow.id" :value="flow.id">{{ flow.name }}（{{ flow.steps.length }} 步）<template v-if="draft.id === flow.id && dirty"> · 编辑中</template></option>
                </optgroup>
              </PixelControl>
            </label>
            <button type="button" class="fl-mini" :disabled="locked" @click="newFlow">＋ 新建</button>
            <button type="button" class="fl-mini" :disabled="locked || !draft.id" aria-haspopup="true" :aria-label="`${draft.name || '这条流程'}的更多操作`" @click="openFlowMenu($event)">⋯ 更多</button>
            <small class="fl-flowbar-note">拼新活动的姿势：复制一条旧的，改改就能跑。</small>
          </div>
          <p v-if="warnings.length" class="fl-warn" role="alert">{{ warnings.join('；') }}</p>
          <p v-if="isOfficial" class="fl-official-note">「{{ draft.name }}」是<strong>官方样板流程</strong>：像展品一样只读展示，可以直接运行、可以单步试跑。想改成自己的——点「⋯ 更多 → 复制成我的流程」。</p>
          <header class="fl-editor-head">
            <label class="fl-name">{{ draft.id ? (isOfficial ? '官方流程名称' : '流程名称') : '新的流程' }}<PixelControl v-model="draft.name" maxlength="30" :disabled="isOfficial" placeholder="给流程起个名字，比如：新活动每日一抽" /></label>
          </header>
          <div class="fl-list-heading"><h3>步骤 <span>{{ draft.steps.length }} / {{ maxSteps }}</span></h3><small>从上往下依次执行</small></div>
          <div v-if="!draft.steps.length" class="fl-empty">
            <h3>想让它怎么动？</h3><p>先用「认」找到东西，再用「点」点它，路线交给「结构」。</p>
            <button type="button" class="fl-button fl-primary" @click="openPicker(0)">添加第一个步骤</button>
          </div>
          <div v-else class="fl-steps">
            <template v-for="(step, index) in draft.steps" :key="step.id">
              <button type="button" class="fl-insert" :disabled="locked || isOfficial || draft.steps.length >= maxSteps" :aria-label="`在第 ${index + 1} 步前插入`" @click="openPicker(index)"><span>＋ 在这里插入</span></button>
              <article :id="`fl-step-${step.id}`" class="fl-step" :class="{ 'is-open': expanded === step }">
                <div class="fl-step-head">
                  <button type="button" class="fl-step-toggle" :aria-expanded="expanded === step" @click="expanded = expanded === step ? null : step">
                    <span class="fl-number">{{ String(index + 1).padStart(2, '0') }}</span>
                    <span class="fl-step-label">
                      <strong><em class="fl-badge" :class="`cat-${categoryBadge[defOf(step.type)?.category || '点']}`">{{ defOf(step.type)?.category || '?' }}</em>{{ stepLabel(step) }}</strong>
                      <span>{{ summary(step) }}</span>
                    </span>
                    <span class="fl-chevron" aria-hidden="true">{{ expanded === step ? '−' : '＋' }}</span>
                  </button>
                  <span class="fl-step-tools">
                    <button type="button" :aria-label="`试跑第 ${index + 1} 步`" :title="pointPicking && expanded === step ? '画布点选坐标中' : '单步试跑（只认不点）'" :disabled="probingStep === step" @click="testStep(step)">{{ probingStep === step ? '…' : '▶' }}</button>
                    <button type="button" :aria-label="`上移第 ${index + 1} 步`" title="上移" :disabled="locked || isOfficial || index === 0" @click="moveStep(index, -1)">↑</button>
                    <button type="button" :aria-label="`下移第 ${index + 1} 步`" title="下移" :disabled="locked || isOfficial || index === draft.steps.length - 1" @click="moveStep(index, 1)">↓</button>
                    <button type="button" class="fl-danger" :aria-label="`移除第 ${index + 1} 步`" title="移除步骤" :disabled="locked || isOfficial" @click="removeStep(index)">×</button>
                  </span>
                </div>
                <div v-if="expanded === step" class="fl-step-detail">
                 <fieldset :disabled="isOfficial" class="fl-guard">
                  <p>{{ defOf(step.type)?.desc }}</p>
                  <p v-if="step.type === 'builtin' && builtinOf(step.params.name)" class="fl-builtin-note">{{ builtinOf(step.params.name)?.desc }}</p>
                  <div class="fields fl-params">
                    <template v-for="field in editorFields(step)" :key="field.key">
                      <ParamField v-if="['select', 'number', 'text', 'toggle', 'checks'].includes(field.type)" :field="field" :model-value="valueFor(step, field.key)" @update:model-value="step.params[field.key] = $event" />
                      <div v-else-if="field.type === 'template'" class="fl-field">
                        <span class="fl-field-label">{{ field.label }}<button v-if="field.help" type="button" class="fl-help" :aria-label="`${field.label}说明`">?<span role="tooltip">{{ field.help }}</span></button></span>
                        <span class="fl-template-pick">
                          <PixelControl :model-value="String(valueFor(step, field.key) ?? '')" list="fl-template-options" placeholder="选模板路径，或输入 @逻辑名" @update:model-value="step.params[field.key] = $event" />
                          <img v-if="templatePreview(step, field)" class="fl-template-preview" :src="api.flowLabTemplateImageUrl(templatePreview(step, field))" :alt="templatePreview(step, field)" />
                        </span>
                        <datalist id="fl-template-options">
                          <option v-for="tpl in templates" :key="tpl" :value="tpl">{{ tpl }}</option>
                        </datalist>
                      </div>
                      <div v-else-if="field.type === 'roi'" class="fl-field">
                        <span class="fl-field-label">{{ field.label }}<button v-if="field.help" type="button" class="fl-help" :aria-label="`${field.label}说明`">?<span role="tooltip">{{ field.help }}</span></button></span>
                        <span class="fl-roi-row">
                          <code>{{ roiDisplay(step, field) }}</code>
                          <button v-if="roiValue(step, field) !== ''" type="button" class="fl-mini" @click="clearRoi(step, field)">清除</button>
                        </span>
                        <span v-if="savedRois.length" class="fl-roi-row">
                          <PixelControl as="select" model-value="" @update:model-value="useRoiFromStore(step, field, String($event))">
                            <option value="" disabled>引用模板工坊框好的 ROI…</option>
                            <option v-for="roi in savedRois" :key="roi.name" :value="roi.name">{{ roi.name }}（{{ roi.w }}×{{ roi.h }}）</option>
                          </PixelControl>
                        </span>
                        <span class="fl-roi-row">
                          <PixelControl placeholder="或填素材包 ROI 名（@开头）" :model-value="typeof roiValue(step, field) === 'string' ? roiValue(step, field) as string : ''" @update:model-value="setRoiRef(step, field, $event)" />
                        </span>
                        <span class="fl-hintline">展开这步后在右栏画布上拖框自动回填；填 @名字 直接用素材包里的识别区域。</span>
                      </div>
                      <div v-else-if="field.type === 'step'" class="fl-field">
                        <span class="fl-field-label">{{ field.label }}<button v-if="field.help" type="button" class="fl-help" :aria-label="`${field.label}说明`">?<span role="tooltip">{{ field.help }}</span></button></span>
                        <PixelControl as="select" :model-value="String(valueFor(step, field.key) ?? '')" @update:model-value="step.params[field.key] = $event">
                          <option value="" disabled>选要跳到的步骤</option>
                          <option v-for="target in draft.steps" :key="target.id" :value="target.id">{{ target.id }} · {{ stepLabel(target) }}</option>
                        </PixelControl>
                      </div>
                    </template>
                  </div>
                  <div v-if="step.type === 'click_point'" class="fl-field fl-named-point">
                    <span class="fl-field-label">命名点（素材包）</span>
                    <PixelControl :model-value="String(step.params.point ?? '')" placeholder="@命名点，留空则用手填坐标" @update:model-value="setNamedPoint(step, $event)" />
                    <span class="fl-hintline">填了命名点就按素材包里登记的坐标点，不再显示画布拾取；留空就用下面的 x/y 坐标。</span>
                  </div>
                  <div v-if="step.type === 'swipe' || (step.type === 'click_point' && !hasNamedPoint(step))" class="fl-point-tools">
                    <span>在画面里点坐标：</span>
                    <button v-if="step.type === 'click_point'" type="button" class="fl-mini" :class="{ active: pointPicking && expanded === step }" @click="startPointPicking('single')">在画布上点一下</button>
                    <template v-else>
                      <button type="button" class="fl-mini" :class="{ active: pointPicking && pointRole === 'start' && expanded === step }" @click="startPointPicking('start')">点起点</button>
                      <button type="button" class="fl-mini" :class="{ active: pointPicking && pointRole === 'end' && expanded === step }" @click="startPointPicking('end')">点终点</button>
                    </template>
                    <span v-if="pointPicking && expanded === step" class="fl-hintline">点画中…按 Esc 取消。</span>
                  </div>
                  <p class="fl-fail-policy">翻车处置不用逐步配：这步没跑成会记一笔——能安全回本丸就收拾残局继续跑，安全拦截（比如重伤保护）照样停下等你。</p>
                 </fieldset>
                </div>
              </article>
            </template>
            <button v-if="!isOfficial" type="button" class="fl-button fl-add" :disabled="locked || draft.steps.length >= maxSteps" @click="openPicker(draft.steps.length)">{{ draft.steps.length >= maxSteps ? '已达到 50 步上限' : '＋ 添加下一步' }}</button>
            <p v-else class="fl-official-note fl-official-tail">官方样板到此为止——复制成我的流程就能接着拼。</p>
          </div>
        </fieldset>
        <footer class="fl-toolbar">
          <span class="fl-save-state" :class="{ unsaved: dirty }">{{ isOfficial ? '官方流程只读' : dirty ? '● 尚未保存' : '✓ 已保存' }}</span>
          <div class="fl-actions"><button v-if="!isOfficial" type="button" class="fl-button" :disabled="!dirty || !valid || locked" @click="save">{{ saving ? '保存中…' : '保存流程' }}</button><button type="button" class="fl-button fl-primary" :disabled="locked || !valid" @click="run">{{ starting ? '正在启动…' : !isOfficial && dirty ? '保存并运行' : '运行这条' }}<span aria-hidden="true"> →</span></button></div>
        </footer>
        <p v-if="message" class="fl-message" :class="{ 'fl-danger': failed }" :role="failed ? 'alert' : 'status'">{{ message }}</p>
      </PaperCard>

      <aside class="fl-stage" aria-label="调试画布">
        <section class="fl-card">
          <h3>画面 <small>取模板工坊的会话帧，也可以现抓一帧</small></h3>
          <div class="fl-row">
            <label>会话<PixelControl v-model="historyPick" as="select">
              <option value="" disabled>选一组帧</option>
              <option v-for="s in sessions" :key="s.id" :value="s.id">{{ s.memo ? `${s.memo} · ` : '' }}{{ s.id }}（{{ s.frames.length }} 帧）</option>
            </PixelControl></label>
            <label>帧<PixelControl v-model="frameIdx" as="select">
              <option v-for="(f, i) in frames" :key="f.idx" :value="i">#{{ f.idx }}</option>
            </PixelControl></label>
            <button type="button" class="fl-mini" :disabled="capturing" @click="captureOne">{{ capturing ? '抓取中…' : '抓一帧' }}</button>
            <div class="fl-zoom" role="group" aria-label="缩放">
              <button v-for="z in [1, 2, 4]" :key="z" type="button" :class="{ active: zoom === z }" @click="zoom = z">{{ z * 100 }}%</button>
            </div>
          </div>
          <div ref="scrollRef" class="fl-canvas-scroll" data-stage-scroll-ignore>
            <canvas ref="canvasRef" class="fl-canvas" :style="canvasStyle" @mousedown.prevent="onMouseDown" @mousemove="onCanvasHover" />
          </div>
          <p v-if="!sessions.length" class="fl-hintline">还没有帧——点「抓一帧」，或去模板工坊抓一组。</p>
          <p v-else class="fl-hintline">{{ pointPicking ? '点选模式：在画面上点一下回填坐标（Esc 取消）' : `拖框 = 给展开步骤的 ROI 画范围（${selText}）；方向键微调 1px，Shift=10px。100% 恰好填满右栏，200%/400% 在框内滚动` }}</p>
        </section>
        <section v-if="probe" class="fl-card fl-probe">
          <h3>试跑结果 <small>{{ probe.kind === 'recognize' ? '只认不点' : '动作预览，没有真点' }}</small></h3>
          <template v-if="probe.kind === 'recognize'">
            <p>判定：<b :class="probe.hit ? 'fl-ok' : 'fl-bad'">{{ probe.hit ? '认到了' : '没认到' }}</b>
              <template v-if="probe.score != null"> · 分数 {{ probe.score }}</template>
              <template v-if="probe.point"> · 位置 ({{ probe.point[0] }}, {{ probe.point[1] }})</template></p>
            <p v-if="probe.texts && probe.texts.length">读到的字：{{ probe.texts.join('、') }}</p>
            <p v-if="!probe.hit" class="fl-hintline">调调阈值、把框画准一点，或换文字/模板再试。</p>
          </template>
          <template v-else>
            <p v-if="probe.action === 'click'">将要点：({{ probe.point?.[0] ?? '认不到' }}, {{ probe.point?.[1] ?? '—' }})<b v-if="probe.hit === false" class="fl-bad">（现在画面里认不到，真跑会翻车）</b></p>
            <p v-else-if="probe.action === 'swipe'">将要滑：({{ probe.from?.[0] }}, {{ probe.from?.[1] }}) → ({{ probe.to?.[0] }}, {{ probe.to?.[1] }})，{{ probe.duration_ms }}ms</p>
            <p v-else-if="probe.action === 'sleep'">将要睡 {{ probe.seconds }} 秒</p>
            <p v-if="probe.note" class="fl-hintline">{{ probe.note }}</p>
          </template>
          <p class="fl-hintline">标记画在画布上——试跑认的是刚抓的新帧，画布可能是旧帧，位置供参考。</p>
        </section>
      </aside>
    </div>

    <div ref="moreMenu" popover="auto" class="fl-preset-menu" :style="menuPosition" :aria-label="`${menuFlow?.name || '流程'}的操作`">
      <template v-if="menuFlow">
        <button v-if="menuFlow.official" type="button" :disabled="locked" @click="copyCurrent">复制成我的流程</button>
        <template v-else>
          <button type="button" :disabled="locked" @click="duplicate(menuFlow)">复制流程</button>
          <button type="button" :disabled="locked" @click="renameFlow">改名…</button>
          <button type="button" class="fl-danger" :disabled="locked" @click="remove(menuFlow)">删除流程</button>
        </template>
      </template>
    </div>
    <dialog ref="picker" class="fl-dialog fl-picker" aria-labelledby="fl-picker-title" @cancel.prevent="closePicker">
      <header class="fl-dialog-head"><div><h2 id="fl-picker-title">添加步骤</h2><p>按 认 → 点 → 结构 的顺序想：先找到，再动手。</p></div><button type="button" class="fl-close" aria-label="关闭步骤选择" @click="closePicker">×</button></header>
      <div class="fl-search-area"><label class="fl-search"><span aria-hidden="true">⌕</span><input ref="searchInput" v-model="search" type="search" aria-label="搜索步骤" placeholder="搜索步骤，比如：等地标、滑动、跳转" /></label></div>
      <div class="fl-catalog">
        <section v-for="group in groups" :key="group.category">
          <h3><em class="fl-badge" :class="`cat-${categoryBadge[group.category]}`">{{ group.category }}</em>{{ categoryHint[group.category] }}</h3>
          <div class="fl-catalog-grid">
            <button v-for="def in group.steps" :key="def.type" type="button" :disabled="(draft?.steps.length || 0) >= maxSteps" @click="addStep(def)">
              <span><strong>{{ def.label }}</strong><small>{{ def.desc }}</small></span>
              <span class="fl-catalog-plus" aria-hidden="true">＋</span>
            </button>
          </div>
        </section>
        <p v-if="!groups.length" class="fl-no-results">没有找到这个步骤，换个关键词试试。</p>
      </div>
      <footer class="fl-picker-footer"><span role="status">{{ (draft?.steps.length || 0) >= maxSteps ? '已达到 50 步上限' : lastAdded || `将加入第 ${insertAt + 1} 步` }}<small>当前共 {{ draft?.steps.length || 0 }} 个步骤</small></span><button type="button" class="fl-button fl-primary" @click="closePicker">选好了</button></footer>
    </dialog>
    <dialog ref="switchDialog" class="fl-dialog fl-switch" aria-labelledby="fl-switch-title" @close="pendingSwitch = null; flowPick = draft?.id || ''">
      <h2 id="fl-switch-title">先收好这条流程？</h2><p>「{{ draft?.name || '未命名流程' }}」还有没保存的修改。</p><p v-if="failed" class="fl-danger" role="alert">{{ message }}</p><div class="fl-switch-actions"><button type="button" class="fl-text-button" :disabled="locked" @click="switchDialog?.close()">继续编辑</button><button type="button" class="fl-button" :disabled="locked" @click="confirmSwitch(false)">放弃修改</button><button type="button" class="fl-button fl-primary" :disabled="!valid || locked" @click="confirmSwitch(true)">{{ saving ? '保存中…' : '保存并切换' }}</button></div>
    </dialog>
  </section>
</template>

<style scoped>
.flow-lab { color: var(--ink); min-width: 0; }
.flow-lab button { cursor: pointer; font: inherit; }
.flow-lab button:disabled { cursor: default; opacity: .42; }
.flow-lab button:focus-visible, .flow-lab input:focus-visible, .flow-lab select:focus-visible { outline: 2px solid var(--fox-gold); outline-offset: 3px; }
.fl-loading { padding: 30px clamp(22px, 4vw, 58px); color: var(--ink-dim); font-size: 13px; }
.fl-layout { display: grid; grid-template-columns: minmax(360px, 1fr) minmax(0, 420px); gap: 0; align-items: start; padding: 0 clamp(16px, 3vw, 44px); }
.fl-button { min-height: 38px; border: 1px solid var(--paper-line); border-radius: 6px; color: var(--ink); background: var(--paper-card); padding: 9px 15px; font-size: 13px; font-weight: 600; }
.fl-button:hover:not(:disabled) { border-color: var(--fox-gold); background: var(--paper-panel); }
.fl-button.fl-primary { background: var(--fox-gold); color: #fffaf0; border-color: var(--fox-gold); }
.fl-button.fl-primary:hover:not(:disabled) { filter: brightness(.95); background: var(--fox-gold); }
.fl-editor { margin: 22px 18px 22px 0; align-self: start; padding: 0; min-width: 0; border: 1px solid var(--paper-line); border-radius: var(--r-md); background: var(--paper-card); box-shadow: 0 5px 18px #49382106; overflow: visible; }
.fl-edit-fields { border: 0; margin: 0; padding: 25px 26px 22px; min-width: 0; }
.fl-flowbar { display: flex; align-items: flex-end; gap: 10px; flex-wrap: wrap; padding-bottom: 18px; }
.fl-flowpick { display: flex; flex-direction: column; gap: 5px; font-size: 12px; font-weight: 700; color: var(--ink-dim); min-width: 0; }
.fl-flowpick :deep(.pixel-control) { width: clamp(200px, 26vw, 340px); }
.fl-flowbar-note { font-size: 11px; color: var(--ink-dim); line-height: 1.7; margin-left: auto; max-width: 260px; }
.fl-editor-head { display: flex; gap: 16px; align-items: center; padding-bottom: 24px; }
.fl-name { display: grid; gap: 9px; flex: 1; min-width: 0; color: var(--ink-dim); font-size: 11px; }
.fl-name :deep(input) { width: 100%; min-width: 0; height: auto; border: 0; border-bottom: 1px solid var(--paper-line); border-radius: 0; padding: 8px 0; background: transparent; box-shadow: none; color: var(--ink); font: inherit; font-size: 20px; font-weight: 650; }
.fl-name :deep(input::placeholder) { color: var(--ink-dim); font-size: 16px; font-weight: normal; opacity: .75; }
.fl-preset-menu { position: fixed; inset: auto; margin: 0; width: 136px; padding: 4px; background: var(--paper-card); color: var(--ink); border: 1px solid var(--paper-line); border-radius: 6px; box-shadow: 0 4px 14px #0002; }
.fl-preset-menu button { display: block; width: 100%; background: none; color: var(--ink); border: 0; border-radius: 3px; padding: 8px 10px; text-align: left; font-size: 12px; }
.fl-preset-menu button:hover:not(:disabled) { background: var(--paper-panel); }
.fl-list-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.fl-list-heading h3 { margin: 0; font-size: 13px; }
.fl-list-heading h3 span { margin-left: 8px; color: var(--ink-dim); font-size: 11px; font-weight: normal; }
.fl-list-heading > small { color: var(--ink-dim); font-size: 11px; }
.fl-editor:has(.fl-empty) .fl-edit-fields { padding-block: 18px 10px; }
.fl-editor:has(.fl-empty) .fl-editor-head { padding-bottom: 14px; }
.fl-empty { text-align: center; padding: 16px 10px 18px; }
.fl-empty h3 { font-size: 16px; margin: 0 0 8px; }
.fl-empty p { color: var(--ink-dim); font-size: 12px; margin: 0 0 14px; line-height: 1.8; }
.fl-insert { display: flex; width: 100%; height: 29px; padding: 0; border: 0; align-items: center; justify-content: center; gap: 8px; background: transparent; color: var(--ink-dim); font-size: 10px; }
.fl-insert::before, .fl-insert::after { content: ''; height: 1px; background: var(--paper-line); flex: 1; opacity: .5; }
.fl-insert span { opacity: .65; }
.fl-insert:hover span, .fl-insert:focus-visible span { opacity: 1; color: var(--fox-gold); }
.fl-step { border: 1px solid var(--paper-line); border-radius: var(--r-md); background: var(--paper-card); scroll-margin: 12px; transition: border-color .15s ease; }
.fl-step.is-open { border-color: var(--fox-gold); }
.fl-step-head { display: flex; align-items: center; padding: 0 9px 0 0; }
.fl-step-toggle { display: flex; align-items: center; gap: 13px; min-width: 0; flex: 1; border: 0; background: none; color: var(--ink); padding: 14px 12px; text-align: left; }
.fl-number { align-self: flex-start; margin-top: 1px; width: 28px; height: 28px; flex-shrink: 0; display: grid; place-items: center; color: var(--fox-gold); background: var(--paper-panel); border-radius: 4px; font-size: 11px; font-variant-numeric: tabular-nums; }
.fl-step-label { min-width: 0; flex: 1; overflow-wrap: anywhere; }
.fl-step-label strong { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; font-size: 14px; line-height: 1.7; }
.fl-step-label strong small { font-weight: normal; color: var(--ink-dim); font-size: 10px; border: 1px solid var(--paper-line); padding: 0 5px; border-radius: 3px; }
.fl-step-label > span { display: block; color: var(--ink-dim); font-size: 11px; line-height: 1.7; margin-top: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fl-chevron { color: var(--ink-dim); font-size: 13px; }
.fl-badge { font-style: normal; font-size: 10px; font-weight: 600; padding: 1px 6px; border-radius: 3px; border: 1px solid var(--paper-line); color: var(--ink-dim); }
.fl-badge.cat-see { color: #426047; border-color: #42604766; background: #42604714; }
.fl-badge.cat-tap { color: var(--fox-gold-deep, #b3781f); border-color: #b3781f66; background: #b3781f14; }
.fl-badge.cat-ctrl { color: #6b5b9e; border-color: #6b5b9e66; background: #6b5b9e14; }
.fl-step-tools { display: flex; border-left: 1px solid var(--paper-line); padding-left: 7px; }
.fl-step-tools button { border: 0; background: none; color: var(--ink-dim); width: 29px; height: 34px; border-radius: 4px; font-size: 12px; }
.fl-step-tools button:hover:not(:disabled) { background: var(--paper-panel); color: var(--ink); }
.fl-guard { border: 0; margin: 0; padding: 0; min-width: 0; }
.fl-official-note { margin: 0 0 14px; padding: 10px 14px; background: #42604710; border: 1px dashed #42604766; border-radius: 7px; color: #426047; font-size: 12px; line-height: 1.8; }
.fl-official-tail { margin: 14px 0 0; text-align: center; }
.fl-warn { margin: -6px 0 12px; padding: 8px 12px; background: #b3781f12; border: 1px solid #b3781f55; border-radius: 7px; color: var(--fox-gold-deep, #b3781f); font-size: 12px; line-height: 1.7; }
.fl-step-detail { padding: 0 18px 18px 53px; }
.fl-step-detail > p { font-size: 12px; color: var(--ink-dim); line-height: 1.7; margin: 0 0 14px; }
.fl-builtin-note { padding: 9px 12px; background: var(--paper-panel); border-radius: 5px; }
.fl-params { padding: 0; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.fl-field { display: grid; gap: 7px; align-content: start; min-width: 0; }
.fl-field-label { color: var(--ink-dim); font-size: 12px; display: flex; gap: 6px; align-items: center; }
.fl-help { position: relative; border: 1px solid var(--paper-line); border-radius: 50%; width: 16px; height: 16px; font-size: 10px; color: var(--ink-dim); background: none; padding: 0; }
.fl-help span { display: none; position: absolute; left: 50%; bottom: 130%; transform: translateX(-50%); width: 230px; padding: 9px 11px; background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 7px; box-shadow: 0 6px 18px #0002; color: var(--ink); font-size: 11px; line-height: 1.7; z-index: 5; }
.fl-help:hover span, .fl-help:focus-visible span { display: block; }
.fl-template-pick { display: flex; gap: 10px; align-items: center; min-width: 0; flex-wrap: wrap; }
.fl-template-pick :deep(.pixel-control) { flex: 1; min-width: 150px; }
.fl-template-preview { height: 40px; max-width: 120px; object-fit: contain; border: 1px solid var(--paper-line); border-radius: 4px; image-rendering: pixelated; background: #221c17; }
.fl-roi-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.fl-roi-row code { font-size: 11px; color: var(--ink); background: var(--paper-panel); padding: 4px 8px; border-radius: 4px; }
.fl-roi-row :deep(.pixel-control) { flex: 1; min-width: 150px; }
.fl-mini { border: 1px solid var(--paper-line); border-radius: 5px; background: var(--paper-card); color: var(--ink); padding: 5px 10px; font-size: 11px; }
.fl-mini:hover:not(:disabled) { border-color: var(--fox-gold); }
.fl-mini.active { border-color: var(--fox-gold); color: var(--fox-gold); background: var(--paper-panel); }
.fl-hintline { font-size: 11px; color: var(--ink-dim); line-height: 1.7; margin: 0; }
.fl-point-tools { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 4px; }
.fl-point-tools > span:first-child { font-size: 12px; color: var(--ink-dim); }
.fl-named-point { margin-top: 12px; }
.fl-fail-policy { margin: 15px 0 0; padding-top: 14px; border-top: 1px dashed var(--paper-line); font-size: 12px; color: var(--ink-dim); line-height: 1.8; }
.fl-add { display: block; width: 100%; margin-top: 18px; border-style: dashed; background: transparent; color: var(--fox-gold); }
.fl-toolbar { display: flex; position: sticky; bottom: 0; z-index: 3; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; padding: 15px 26px; background: var(--paper-card); border-top: 1px solid var(--paper-line); border-radius: 0 0 10px 10px; box-shadow: 0 -6px 14px #49382104; }
.fl-save-state { font-size: 11px; color: var(--ink-dim); }
.fl-save-state.unsaved { color: var(--fox-gold); }
.fl-actions { display: flex; gap: 9px; }
.fl-message { margin: 0; padding: 12px 26px; font-size: 12px; }
.fl-danger { color: #a04b3a; }
button.fl-danger { color: #a04b3a; }
.fl-ok { color: #426047; }
.fl-bad { color: #9f3d28; }
.fl-stage { display: grid; gap: 14px; align-content: start; min-width: 0; padding: 22px 0; }
.fl-card { display: grid; gap: 10px; align-content: start; padding: 14px 16px; background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: var(--r-lg); min-width: 0; }
.fl-card h3 { margin: 0; font-size: 15px; }
.fl-card h3 small { margin-left: 8px; color: var(--ink-dim); font-size: 11px; font-weight: 400; }
.fl-row { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 10px; }
.fl-row label { display: flex; flex-direction: column; gap: 5px; font-size: 12px; font-weight: 700; min-width: 0; }
.fl-row label :deep(.pixel-control) { width: 130px; }
.fl-row label:first-child :deep(.pixel-control) { width: 170px; }
.fl-zoom { display: flex; gap: 0; border: 1px solid var(--line-strong); border-radius: var(--r-sm); overflow: hidden; margin-left: auto; }
.fl-zoom button { padding: 7px 10px; background: var(--paper); border: 0; border-right: 1px solid var(--line-strong); font-size: 11px; }
.fl-zoom button:last-child { border-right: 0; }
.fl-zoom button.active { color: #fffaf0; background: var(--fox-gold-deep); }
.fl-canvas-scroll { overflow: auto; max-height: 46vh; min-width: 0; border: 1px solid var(--paper-line); border-radius: 6px; background: #221c17; }
.fl-canvas { display: block; cursor: crosshair; image-rendering: pixelated; }
.fl-probe p { margin: 0; font-size: 12px; line-height: 1.8; }
.fl-dialog { padding: 0; border: 1px solid var(--paper-line); border-radius: 12px; background: var(--paper-card); color: var(--ink); width: min(700px, calc(100% - 32px)); max-height: calc(100dvh - 48px); box-shadow: 0 22px 80px #251a1140; }
.fl-dialog::backdrop { background: #30291f80; }
.fl-picker[open] { display: flex; flex-direction: column; }
.fl-dialog-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding: 25px 26px 18px; }
.fl-dialog h2 { font-size: 20px; margin: 0; }
.fl-dialog-head p { font-size: 12px; color: var(--ink-dim); margin: 9px 0 0; }
.fl-close { color: var(--ink-dim); font-size: 25px; border: 0; background: none; padding: 0 4px; }
.fl-search-area { padding: 0 26px 17px; border-bottom: 1px solid var(--paper-line); }
.fl-search { display: flex; align-items: center; gap: 10px; padding: 0 12px; border: 1px solid var(--paper-line); background: var(--paper); border-radius: var(--r-sm); }
.fl-search > span { color: var(--ink-dim); font-size: 25px; }
.fl-search input { width: 100%; min-width: 0; background: none; border: 0; padding: 12px 0; color: var(--ink); font: inherit; font-size: 13px; }
.fl-catalog { overflow-y: auto; min-height: 0; padding: 2px 26px 24px; overscroll-behavior: contain; }
.fl-catalog h3 { display: flex; align-items: center; gap: 8px; margin: 21px 0 10px; font-size: 12px; color: var(--ink-dim); font-weight: 500; }
.fl-catalog-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
.fl-catalog-grid > button { display: flex; align-items: center; justify-content: space-between; gap: 12px; text-align: left; padding: 14px; color: var(--ink); border: 1px solid var(--paper-line); border-radius: 7px; background: var(--paper-card); min-width: 0; }
.fl-catalog-grid > button:hover:not(:disabled) { border-color: var(--fox-gold); background: var(--paper-panel); }
.fl-catalog-grid strong { display: block; font-size: 13px; font-weight: 600; }
.fl-catalog-grid small { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; margin-top: 7px; font-size: 11px; color: var(--ink-dim); line-height: 1.6; }
.fl-catalog-plus { flex-shrink: 0; color: var(--fox-gold); font-size: 18px; }
.fl-no-results { padding: 30px 0; text-align: center; font-size: 13px; color: var(--ink-dim); }
.fl-picker-footer { flex-shrink: 0; border-top: 1px solid var(--paper-line); padding: 16px 26px; display: flex; align-items: center; justify-content: space-between; gap: 18px; }
.fl-picker-footer > span { font-size: 12px; color: var(--ink); line-height: 1.6; }
.fl-picker-footer small { display: block; color: var(--ink-dim); font-size: 11px; margin-top: 3px; }
.fl-picker-footer button { flex-shrink: 0; }
.fl-switch { width: min(480px, calc(100% - 32px)); padding: 26px; }
.fl-switch > p { color: var(--ink-dim); font-size: 13px; line-height: 1.7; }
.fl-switch-actions { display: flex; justify-content: flex-end; gap: 10px; flex-wrap: wrap; margin-top: 24px; }
.fl-text-button { background: none; border: 0; color: var(--fox-gold); font-size: 12px; padding: 6px 0; }
@media (prefers-reduced-motion: reduce) {
  .fl-step { transition: none; }
}
@media (max-width: 1100px) {
  .fl-layout { grid-template-columns: 1fr; }
  .fl-editor { margin: 22px 0; }
  .fl-stage { grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
  .fl-canvas-scroll { max-height: 60vh; }
}
@media (max-width: 720px) {
  .fl-layout { grid-template-columns: 1fr; padding: 0 14px; }
  .fl-editor { margin: 16px 0; }
  .fl-flowbar-note { display: none; }
  .fl-flowpick :deep(.pixel-control) { width: 100%; }
  .fl-flowpick { flex: 1 1 100%; }
  .fl-stage { padding: 0 0 18px; grid-template-columns: 1fr; }
  .fl-editor-head { padding-bottom: 20px; gap: 8px; }
  .fl-name :deep(input) { font-size: 18px; }
  .fl-name :deep(input::placeholder) { font-size: 13px; }
  .fl-edit-fields { padding: 18px 12px; }
  .fl-empty { padding: 26px 0; }
  .fl-empty h3 { font-size: 15px; }
  .fl-step-head { flex-wrap: wrap; padding: 0; }
  .fl-step-toggle { padding: 12px 10px; gap: 9px; width: 100%; flex-basis: 100%; }
  .fl-number { width: 23px; height: 25px; }
  .fl-step-tools { margin: 0 8px 6px auto; border: 0; gap: 4px; }
  .fl-step-tools button { width: 36px; height: 32px; background: var(--paper-panel); }
  .fl-step-detail { padding: 8px 12px 15px; }
  .fl-params { grid-template-columns: 1fr; }
  .fl-toolbar { padding: 12px; gap: 8px; }
  .fl-save-state { width: 100%; }
  .fl-actions { width: 100%; }
  .fl-actions button { flex: 1; padding-inline: 8px; }
  .fl-message { padding: 12px; }
  .fl-dialog { max-height: calc(100dvh - 24px); width: calc(100% - 20px); border-radius: 9px; }
  .fl-dialog-head { padding: 20px 16px 16px; }
  .fl-search-area { padding: 0 16px 14px; }
  .fl-catalog { padding: 0 16px 18px; }
  .fl-catalog-grid { grid-template-columns: 1fr; }
  .fl-picker-footer { padding: 12px 16px; gap: 10px; }
  .fl-picker-footer > span { font-size: 11px; }
  .fl-switch { padding: 22px 18px; }
}
</style>
