<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'
import type { ExpeditionSchedule, ExpeditionSlotState, ExpeditionSlotStatus } from '../types'
import PanelHeader from './PanelHeader.vue'
import PaperCard from './PaperCard.vue'
import PixelControl from './PixelControl.vue'

withDefaults(defineProps<{ embedded?: boolean }>(), { embedded: false })

const config = ref<ExpeditionSchedule | null>(null)
const message = ref('')
const teams = [1, 2, 3, 4, 5]
const autoTeams = computed<number[]>({ get: () => config.value?.automation?.teams || [2, 3, 4], set: value => { if (config.value) config.value.automation.teams = value } })
const teamNames = ['', '部队一', '部队二', '部队三', '部队四', '部队五']

// 班次状态 → chip 文案/颜色。等待确认占用附上原因，已派出附上晚了几分钟。
function chipFor(item: ExpeditionSlotStatus | undefined): { text: string; cls: string } | null {
  if (!item) return null
  switch (item.state as ExpeditionSlotState) {
    case 'waiting_busy': return { text: '等自家任务收工', cls: 'is-waiting' }
    case 'waiting_unknown': return { text: `等待确认占用${item.blocked_reason ? `（${item.blocked_reason}）` : ''}`, cls: 'is-waiting' }
    case 'ready': return { text: '即将接管', cls: 'is-ready' }
    case 'dispatched': return { text: item.late_min ? `已派出（晚${item.late_min}分钟）` : '已派出', cls: 'is-done' }
    case 'expired': return { text: '已过期跳过', cls: 'is-expired' }
    case 'failed_unknown': return { text: `派遣停止${item.blocked_reason ? `（${item.blocked_reason}）` : ''}`, cls: 'is-failed' }
    case 'missed': return { text: '错过（面板没在线）', cls: 'is-missed' }
    default: return { text: '待点', cls: 'is-pending' }
  }
}

const presetStatus = computed(() => {
  const map = new Map<string, ExpeditionSlotStatus>()
  for (const item of config.value?.today?.preset || []) map.set(`${item.lane}:${item.offset_min}`, item)
  return map
})
const customStatus = computed(() => {
  const map = new Map<number, ExpeditionSlotStatus>()
  for (const item of config.value?.today?.custom || []) map.set(item.index, item)
  return map
})

const presetTimeline = computed(() => {
  const preset = config.value?.presets?.[config.value?.automation?.preset || '']
  if (!preset || !config.value) return []
  const [hours, minutes] = String(config.value.automation.start_time || '08:00').split(':').map(Number)
  const start = hours * 60 + minutes
  return (preset.lanes || []).map((lane, laneIndex) => ({
    team: teamNames[autoTeams.value[laneIndex]] || `部队${autoTeams.value[laneIndex]}`,
    entries: lane.map(part => {
      const minute = (start + Number(part.offset_min || 0)) % 1440
      return {
        time: `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`,
        map: part.map_code,
        chip: chipFor(presetStatus.value.get(`${laneIndex}:${part.offset_min}`)),
      }
    }),
  }))
})
function mapLabel(map: { code: string; name: string; duration_text: string }) { return `${map.code} · ${map.name}（${map.duration_text}）` }
function addEntry() { config.value?.entries.push({ time: '06:40', team_no: 5, map_code: config.value.maps[0]?.code || '', enabled: true, last_fired: '' }) }
function addCommonRows() {
  if (!config.value) return
  const rows = new Map((config.value.common_plan || []).map((row) => [Number(row.team_no), row]))
  config.value.common_plan = teams.map(team => rows.get(team) || { team_no: team, map_code: config.value!.maps[0]?.code || '', enabled: false })
}
async function load() { config.value = await api.expeditionSchedule(); addCommonRows() }
// 实况刷新只换 today 投影，不动用户正在编辑的表单
async function refreshToday() { if (!config.value) return; config.value.today = (await api.expeditionSchedule()).today }
let timer: ReturnType<typeof setInterval> | undefined
async function save() {
  if (!config.value) return
  if (new Set(autoTeams.value).size !== autoTeams.value.length) { message.value = '三条自动路线不能选择重复部队'; return }
  await api.saveExpeditionSchedule({ entries: config.value.entries, common_plan: config.value.common_plan, automation: config.value.automation })
  message.value = '远征安排已保存'
}
async function pause(minutes: number) { if (!config.value) return; const result = await api.pauseExpeditions(minutes); config.value.automation.paused_until = result.paused_until; message.value = result.paused_until ? `已暂停至 ${result.paused_until}` : '自动排班已恢复' }
onMounted(() => { load(); timer = setInterval(() => { refreshToday().catch(() => {}) }, 15000) })
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <section v-if="config" class="schedule-panel">
    <PanelHeader :variant="embedded ? 'embedded' : 'section'" title="自动排班" subtitle="到点派遣集中管理"><template #actions><button class="primary" @click="save">保存排班设置</button></template></PanelHeader>
    <div class="schedule-body">
      <PaperCard variant="settings" class="auto-schedule-card"><div class="auto-card-head"><div><h3>自动排班</h3><p>到点后，自动接管已经打开的游戏并派出远征。面板、模拟器和游戏需要保持开启。</p></div><label class="enable-row"><input v-model="config.automation.enabled" type="checkbox" /> 启用自动排班</label></div>
        <label class="capitalist-row"><input v-model="config.automation.sakura_before_dispatch" type="checkbox" /> 远征续派前补花 <small>常用安排与自动排班都会补花；伤势条件沿用刷花配置</small></label>
        <div class="auto-steps" :class="{ disabled: !config.automation.enabled }">
          <section class="auto-step"><span class="step-number">1</span><div><h4>什么时候开始？</h4><label class="stacked-field">每天从这个时间开始<PixelControl v-model="config.automation.start_time" type="time" /></label><label class="stacked-field">每班最多晚多久（分钟）<PixelControl v-model="config.automation.max_delay_min" type="number" numeric min="1" max="1440" /></label><p class="field-help">到点没派成的班会重试到超过这个延迟为止，之后明确跳过、绝不突然补跑；资本家模式的补跑窗口按它的 4 倍封顶。</p></div></section>
          <section class="auto-step"><span class="step-number">2</span><div><h4>怎么安排？</h4><label class="stacked-field">排班方式<PixelControl v-model="config.automation.mode" as="select"><option value="preset">按资源目标自动安排</option><option value="custom">自己指定时间和地图</option></PixelControl></label>
            <template v-if="config.automation.mode === 'preset'"><label class="stacked-field">想优先带回什么？<PixelControl v-model="config.automation.preset" as="select"><option v-for="(_, name) in config.presets" :key="name" :value="name">{{ name }}</option></PixelControl></label><p class="preset-total">预计一天获得：{{ config.presets[config.automation.preset]?.totals }}</p></template>
          </div></section>
          <section v-if="config.automation.mode === 'preset'" class="auto-step"><span class="step-number">3</span><div><h4>使用哪些部队？</h4><div class="route-teams"><label v-for="(_, index) in autoTeams" :key="index">第{{ index + 1 }}路<PixelControl v-model="autoTeams[index]" as="select" numeric><option v-for="team in teams" :key="team" :value="team">部队{{ team }}</option></PixelControl></label></div><label class="capitalist-row"><input v-model="config.automation.capitalist" type="checkbox" /> 资本家模式 <small>错过一班时补跑并顺延后续安排</small></label><div class="timeline-preview"><h5>今天的派遣时间表</h5><div v-for="lane in presetTimeline" :key="lane.team" class="timeline-lane"><strong>{{ lane.team }}</strong><div><span v-for="entry in lane.entries" :key="`${entry.time}-${entry.map}`"><b>{{ entry.time }}</b> {{ entry.map }} <em v-if="entry.chip" class="slot-chip" :class="entry.chip.cls">{{ entry.chip.text }}</em></span></div></div></div></div></section>
          <section v-else class="auto-step"><span class="step-number">3</span><div class="custom-routes"><h4>设置派出时间</h4><div v-for="(entry, index) in config.entries" :key="index" class="custom-entry"><PixelControl v-model="entry.time" type="time" /><PixelControl v-model="entry.team_no" as="select" numeric><option v-for="team in teams" :key="team" :value="team">部队{{ team }}</option></PixelControl><PixelControl v-model="entry.map_code" as="select"><option v-for="map in config.maps" :key="map.code" :value="map.code">{{ mapLabel(map) }}</option></PixelControl><label><input v-model="entry.enabled" type="checkbox" />启用</label><em v-if="chipFor(customStatus.get(index))" class="slot-chip" :class="chipFor(customStatus.get(index))!.cls">{{ chipFor(customStatus.get(index))!.text }}</em><button class="danger" @click="config.entries.splice(index, 1)">删除</button></div><button class="secondary" @click="addEntry">＋ 添加一行</button></div></section>
        </div>
        <div class="pause-row"><b>临时不接管</b><span>{{ config.automation.paused_until ? `已暂停至 ${config.automation.paused_until}` : '当前正常排班' }}</span><button @click="pause(30)">暂停30分钟</button><button @click="pause(60)">暂停1小时</button><button @click="pause(999)">今天不接管</button><button v-if="config.automation.paused_until" @click="pause(0)">恢复排班</button></div>
      </PaperCard>
      <p v-if="message" class="inline-message">{{ message }}</p>
    </div>
  </section>
</template>
