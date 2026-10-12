<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import PixelControl from './PixelControl.vue'

// 日服出阵任务卡：只在 ?server=jp 的「功能 · 执务台」挂载。
// 日服模式下 App.vue 不轮询脚本状态，这张卡自己盯状态、自己收日志尾行，
// 运行/停止状态通过 v-model 同步给外层，喂执务台的日志面板状态栏。
const emit = defineEmits<{ 'update:running': [value: boolean]; 'update:stopping': [value: boolean] }>()
const TEAM_NAMES = ['部队一', '部队二', '部队三', '部队四', '部队五']
const teamNo = ref('1')
const rounds = ref(1)
const sallyRunning = ref(false)
const otherRunning = ref(false)
const starting = ref(false)
const stopping = ref(false)
const lastLine = ref('')
const lastLineAt = ref(0)
const note = ref('')

const clampRounds = (value: number) => Math.min(200, Math.max(1, Math.round(Number.isFinite(value) ? value : 1)))
const statusText = computed(() => stopping.value ? '正在停止…'
  : sallyRunning.value ? '出阵中，弹窗会停等人工'
  : otherRunning.value ? '另有任务在运行，先等它收工'
  : '空闲')

function snapshot() {
  return { team_no: String(teamNo.value), rounds: clampRounds(rounds.value) }
}

async function persist() {
  try { await api.saveSettings({ 'jp-sally': snapshot() }, 'jp') }
  catch { note.value = '这次选择没能记住，下次打开会还原' }
}

function pickTeam(value: unknown) {
  const next = String(value)
  teamNo.value = TEAM_NAMES.some((_, index) => String(index + 1) === next) ? next : '1'
  void persist()
}

function pickRounds(value: unknown) {
  rounds.value = clampRounds(Number(value))
  void persist()
}

async function refreshStatus() {
  try {
    const state = await api.scripts()
    // 停止请求后后台可能还短暂回报一次“运行中”，真正停稳前不让状态反跳回“出阵中”。
    if (stopping.value && state.running) return
    if (stopping.value && !state.running) stopping.value = false
    sallyRunning.value = state.running && state.current === 'jp-sally'
    otherRunning.value = state.running && state.current !== 'jp-sally'
  } catch { /* 面板暂时没连上，保持原样下轮再试 */ }
}

async function start() {
  if (starting.value || stopping.value || sallyRunning.value || otherRunning.value) return
  starting.value = true
  note.value = ''
  try {
    const result = await api.run('jp-sally', snapshot())
    if (!result.ok) throw new Error('日服出阵没有启动，请重试')
    sallyRunning.value = true
  } catch (error) {
    note.value = error instanceof Error ? error.message : '启动失败，请重试'
  } finally {
    starting.value = false
  }
}

async function stop() {
  if (stopping.value || !sallyRunning.value) return
  stopping.value = true
  try {
    const result = await api.stop()
    if (!result.ok) throw new Error('现在没有正在运行的出阵')
  } catch (error) {
    stopping.value = false
    note.value = error instanceof Error ? error.message : '停止失败，请重试'
  }
}

watch(sallyRunning, value => emit('update:running', value))
watch(stopping, value => emit('update:stopping', value))

function remember(entry: { script?: string; message?: string; ts?: number }) {
  if (entry.script !== 'jp-sally' || !entry.message) return
  lastLine.value = String(entry.message).replace(/^\[[^\]]+\]\s*/, '')
  lastLineAt.value = Number(entry.ts || 0)
}

let pollTimer = 0
let source: EventSource | null = null

function connectLogs() {
  source = new EventSource('/api/logs/stream')
  source.onmessage = event => {
    if (!event.data) return
    try { remember(JSON.parse(event.data)) } catch { /* 跳过坏行 */ }
  }
  source.onerror = () => { source?.close(); source = null; window.setTimeout(connectLogs, 3000) }
}

onMounted(async () => {
  try {
    const saved = await api.settings('jp')
    const savedParams = saved.params?.['jp-sally'] || {}
    if (TEAM_NAMES.some((_, index) => String(index + 1) === String(savedParams.team_no))) teamNo.value = String(savedParams.team_no)
    rounds.value = clampRounds(Number(savedParams.rounds ?? 1))
  } catch { note.value = '上次的选择没能读取，先用默认配置' }
  try {
    const history = await api.logs(200)
    for (const entry of history.logs || []) remember(entry)
  } catch { /* 历史日志读不到就等实时推送 */ }
  await refreshStatus()
  pollTimer = window.setInterval(refreshStatus, 2000)
  connectLogs()
})

onBeforeUnmount(() => {
  window.clearInterval(pollTimer)
  source?.close()
  source = null
})
</script>

<template>
  <section class="jp-sally-card">
    <h3>日服出阵</h3>
    <p>选定部队后按当前记忆的图反复出阵，自动行军。行军中任何弹窗（包括重伤警告）都会停等人工，不会替你点继续行军。</p>
    <div class="sally-fields">
      <label>出阵部队<PixelControl as="select" :model-value="teamNo" @update:model-value="pickTeam"><option v-for="(name, index) in TEAM_NAMES" :key="name" :value="String(index + 1)">{{ name }}</option></PixelControl></label>
      <label>连刷轮数<PixelControl :model-value="rounds" type="number" numeric :min="1" :max="200" @update:model-value="pickRounds" /></label>
    </div>
    <div class="sally-actions">
      <button class="primary" :disabled="starting || stopping || sallyRunning || otherRunning" @click="start">{{ starting ? '出发中…' : '开始出阵' }}</button>
      <button class="danger" :disabled="stopping || !sallyRunning" @click="stop">{{ stopping ? '正在停止…' : '停止' }}</button>
    </div>
    <p class="sally-status" role="status">{{ statusText }}<template v-if="lastLine"> · 最后动静：{{ lastLineAt ? `${new Date(lastLineAt * 1000).toLocaleTimeString()} ` : '' }}{{ lastLine }}</template></p>
    <p v-if="note" class="sally-note">{{ note }}</p>
  </section>
</template>

<style scoped>
.jp-sally-card { margin: 4px 0 20px; padding: 16px 18px; background: var(--paper-panel); border-left: 3px solid var(--fox-gold); }
.jp-sally-card > p { margin: 6px 0; color: var(--ink-dim); font-size: 12px; }
.sally-fields { display: flex; gap: 18px; flex-wrap: wrap; margin: 10px 0; }
.sally-fields label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.sally-actions { display: flex; gap: 10px; margin: 10px 0; }
.sally-note { color: var(--danger, #b4452f); }
@media (max-width: 720px) {
  .jp-sally-card { padding: 14px; }
}
</style>
