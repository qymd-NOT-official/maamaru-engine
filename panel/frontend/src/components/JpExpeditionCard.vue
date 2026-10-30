<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import PixelControl from './PixelControl.vue'

// 日服远征任务卡：执务台显示摘要，玩法设置编辑同一份配置。
// 只派一支部队去指定远征图（选时代 + 小图卡位，图名用于选中确认），
// 出发前验出重伤绝不派遣；不收菜、不排班、不补花。与出阵卡同款
// 自盯状态/自收日志尾行，运行/停止经 v-model 同步给外层喂日志面板。
withDefaults(defineProps<{ settings?: boolean }>(), { settings: false })
const emit = defineEmits<{ configure: []; office: [];  'update:running': [value: boolean]; 'update:stopping': [value: boolean] }>()
const SCRIPT = 'jp-expedition'
const TEAM_NAMES = ['部队一', '部队二', '部队三', '部队四', '部队五']
const ERA_NAMES = ['时代一', '时代二', '时代三', '时代四', '时代五']
const SLOT_NAMES = ['卡位一', '卡位二', '卡位三', '卡位四']
const DEFAULT_MAP_NAME = '鳥羽・伏見'
const teamNo = ref('1')
const era = ref('1')
const mapSlot = ref('1')
const mapName = ref(DEFAULT_MAP_NAME)
const expeditionRunning = ref(false)
const otherRunning = ref(false)
const starting = ref(false)
const stopping = ref(false)
const lastLine = ref('')
const lastLineAt = ref(0)
const note = ref('')

const statusText = computed(() => stopping.value ? '正在停止…'
  : expeditionRunning.value ? '派遣中，验出重伤会立即停'
  : otherRunning.value ? '另有任务在运行，先等它收工'
  : '空闲')

function snapshot() {
  const name = String(mapName.value || '').trim() || DEFAULT_MAP_NAME
  return { team_no: String(teamNo.value), era: String(era.value),
           map_slot: String(mapSlot.value), map_name: name }
}

async function persist() {
  try { await api.saveSettings({ [SCRIPT]: snapshot() }, 'jp') }
  catch { note.value = '这次选择没能记住，下次打开会还原' }
}

function pickTeam(value: unknown) {
  const next = String(value)
  teamNo.value = TEAM_NAMES.some((_, index) => String(index + 1) === next) ? next : '1'
  void persist()
}

function pickEra(value: unknown) {
  const next = String(value)
  era.value = ERA_NAMES.some((_, index) => String(index + 1) === next) ? next : '1'
  void persist()
}

function pickSlot(value: unknown) {
  const next = String(value)
  mapSlot.value = SLOT_NAMES.some((_, index) => String(index + 1) === next) ? next : '1'
  void persist()
}

function pickMapName(value: unknown) {
  mapName.value = String(value ?? '')
  void persist()
}

async function refreshStatus() {
  try {
    const state = await api.scripts()
    // 停止请求后后台可能还短暂回报一次“运行中”，真正停稳前不让状态反跳回“派遣中”。
    if (stopping.value && state.running) return
    if (stopping.value && !state.running) stopping.value = false
    expeditionRunning.value = state.running && state.current === SCRIPT
    otherRunning.value = state.running && state.current !== SCRIPT
  } catch { /* 面板暂时没连上，保持原样下轮再试 */ }
}

async function start() {
  if (starting.value || stopping.value || expeditionRunning.value || otherRunning.value) return
  starting.value = true
  note.value = ''
  try {
    const result = await api.run(SCRIPT, snapshot())
    if (!result.ok) throw new Error('日服远征没有启动，请重试')
    expeditionRunning.value = true
  } catch (error) {
    note.value = error instanceof Error ? error.message : '启动失败，请重试'
  } finally {
    starting.value = false
  }
}

async function stop() {
  if (stopping.value || !expeditionRunning.value) return
  stopping.value = true
  try {
    const result = await api.stop()
    if (!result.ok) throw new Error('现在没有正在运行的派遣')
  } catch (error) {
    stopping.value = false
    note.value = error instanceof Error ? error.message : '停止失败，请重试'
  }
}

watch(expeditionRunning, value => emit('update:running', value))
watch(stopping, value => emit('update:stopping', value))

function remember(entry: { script?: string; message?: string; ts?: number }) {
  if (entry.script !== SCRIPT || !entry.message) return
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
    const savedParams = saved.params?.[SCRIPT] || {}
    if (TEAM_NAMES.some((_, index) => String(index + 1) === String(savedParams.team_no))) teamNo.value = String(savedParams.team_no)
    if (ERA_NAMES.some((_, index) => String(index + 1) === String(savedParams.era))) era.value = String(savedParams.era)
    if (SLOT_NAMES.some((_, index) => String(index + 1) === String(savedParams.map_slot))) mapSlot.value = String(savedParams.map_slot)
    const savedName = String(savedParams.map_name || '').trim()
    mapName.value = savedName || DEFAULT_MAP_NAME
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
  <section class="jp-task-card" :class="settings ? 'task-card' : 'overview-task-card'">
    <header><h2><img :src="'/static/img/ui/expedition.png'" alt="">日服远征</h2></header>
    <p>手动派遣一支部队去指定远征图：选时代和小图卡位，图名用来确认选中了目标图。出发前验出重伤绝不派遣；验伤或结果不明确就停，绝不假绿。不收菜、不排班、不补花。</p>
    <div v-if="!settings" class="overview-summary">
      <span><small>派遣部队</small><b>{{ TEAM_NAMES[Number(teamNo) - 1] }}</b></span>
      <span><small>时代 / 卡位</small><b>{{ ERA_NAMES[Number(era) - 1] }} / {{ SLOT_NAMES[Number(mapSlot) - 1] }}</b></span>
      <span><small>远征图</small><b>{{ mapName.trim() || DEFAULT_MAP_NAME }}</b></span>
    </div>
    <div v-else class="jp-task-fields">
      <label class="field">派遣部队<PixelControl as="select" :model-value="teamNo" @update:model-value="pickTeam"><option v-for="(name, index) in TEAM_NAMES" :key="name" :value="String(index + 1)">{{ name }}</option></PixelControl></label>
      <label class="field">时代<PixelControl as="select" :model-value="era" @update:model-value="pickEra"><option v-for="(name, index) in ERA_NAMES" :key="name" :value="String(index + 1)">{{ name }}</option></PixelControl></label>
      <label class="field">小图卡位<PixelControl as="select" :model-value="mapSlot" @update:model-value="pickSlot"><option v-for="(name, index) in SLOT_NAMES" :key="name" :value="String(index + 1)">{{ name }}</option></PixelControl></label>
      <label class="field">日文图名<PixelControl :model-value="mapName" type="text" placeholder="鳥羽・伏見" @update:model-value="pickMapName" /></label>
    </div>
    <footer>
      <button v-if="!settings" type="button" class="secondary" @click="emit('configure')">调整配置</button>
      <button v-else type="button" class="secondary" @click="emit('office')">返回执务台</button>
      <button class="primary" :disabled="starting || stopping || expeditionRunning || otherRunning" @click="start">{{ starting ? '出发中…' : '开始派遣' }}</button>
      <button class="danger" :disabled="stopping || !expeditionRunning" @click="stop">{{ stopping ? '正在停止…' : '停止' }}</button>
    </footer>
    <p class="jp-task-status" role="status">{{ statusText }}<template v-if="lastLine"> · 最后动静：{{ lastLineAt ? `${new Date(lastLineAt * 1000).toLocaleTimeString()} ` : '' }}{{ lastLine }}</template></p>
    <p v-if="note" class="jp-task-note">{{ note }}</p>
  </section>
</template>
