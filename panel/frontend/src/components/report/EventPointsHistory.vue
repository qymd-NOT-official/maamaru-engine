<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../../api'
import type { EventPointsActivity, EventPointsTimelineResponse } from '../../types'
const events = ref<EventPointsActivity[]>([]), selected = ref('')
const history = ref<EventPointsTimelineResponse>()
const busy = ref(false), error = ref('')
let requestId = 0
const date = (ts: number) => new Date(ts * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
async function load() {
  const id = ++requestId; busy.value = true; error.value = ''; history.value = undefined
  try { const data = await api.eventPoints(selected.value); if (id === requestId) history.value = data }
  catch { if (id === requestId) error.value = '这期活动的记录暂时没有翻开，请重试。' }
  finally { if (id === requestId) busy.value = false }
}
async function open(event: Event) {
  if (!(event.target as HTMLDetailsElement).open || events.value.length || busy.value) return
  busy.value = true; error.value = ''
  try {
    const data = await api.eventPointsList()
    events.value = [...data.events].sort((a, b) => b.start_at.localeCompare(a.start_at))
    selected.value = events.value[0]?.event_id || ''
  } catch { error.value = '活动清单暂时没有翻开，收起后再打开即可重试。' }
  finally { busy.value = false }
  if (selected.value) await load()
}
</script>

<template>
  <details class="event-points-history" @toggle="open">
    <summary>活动点数记录</summary>
    <p class="note">按游戏活动期整理实读点数；未读到的时段不补零。活动名称还未对应时显示编号。</p>
    <label v-if="events.length">活动期 <select v-model="selected" @change="load"><option v-for="event in events" :key="event.event_id" :value="event.event_id">{{ event.start_at.slice(0, 10) }}～{{ event.end_at.slice(0, 10) }} · 活动 {{ event.event_id }}</option></select></label>
    <p v-if="busy">正在翻记录……</p>
    <p v-else-if="error" role="alert">{{ error }} <button v-if="selected" @click="load">重试</button></p>
    <p v-else-if="!events.length">还没有采集到游戏活动清单。</p>
    <template v-else-if="history">
      <p v-if="!history.timeline.length">这期活动暂无点数读数，采集后会逐次积累。</p>
      <template v-else>
        <p>最近记录 <b>{{ history.timeline[history.timeline.length - 1].points.toLocaleString() }}</b> 点 · {{ date(history.timeline[history.timeline.length - 1].ts) }}</p>
        <ol><li v-for="(point, index) in [...history.timeline].reverse()" :key="`${point.ts}-${index}`"><time>{{ point.captured_at || date(point.ts) }}</time><b>{{ point.points.toLocaleString() }} 点</b></li></ol>
      </template>
    </template>
  </details>
</template>

<style scoped>
.event-points-history { background: var(--paper-card); padding: 14px 18px; border: 1px solid var(--paper-line); margin: 12px 0; font-size: 12px; }
summary { cursor: pointer; font-weight: 700; font-size: 14px; }
p { line-height: 1.7; }.note, time { color: var(--ink-dim); }
label { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
select { max-width: 100%; min-width: 0; }
ol { margin: 0; padding: 0; list-style: none; }
li { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; padding: 7px 0; border-top: 1px dashed var(--paper-line); }
</style>
