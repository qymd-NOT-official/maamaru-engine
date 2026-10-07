<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../../api'
import type { DropStatsResponse, ForgeHistoryResponse, ForgeHistoryRecipe } from '../../types'
const props = defineProps<{ server?: 'cn' | 'jp' }>()
const days = ref(30), kind = ref<'drops' | 'forge'>('drops')
const drops = ref<DropStatsResponse>(), forge = ref<ForgeHistoryResponse>()
const shown = ref(12)
const visibleForges = computed(() => [...(forge.value?.forges || [])].reverse().slice(0, shown.value))
const busy = ref(false), error = ref(''), opened = ref(false)
let requestId = 0
const fmt = (n: number | null) => n == null ? '未记录' : n.toLocaleString()
const date = (ts: number) => new Date(ts * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
const recipe = (r: ForgeHistoryRecipe) => [r.charcoal, r.steel, r.coolant, r.file].map(n => n == null ? '？' : n.toLocaleString()).join(' / ')
async function load() {
  shown.value = 12
  const id = ++requestId
  busy.value = true; error.value = ''
  try {
    if (kind.value === 'drops') { const value = await api.dropStats(days.value, props.server); if (id === requestId) drops.value = value }
    else { const value = await api.forgeHistory(days.value, props.server); if (id === requestId) forge.value = value }
  } catch { if (id === requestId) error.value = '记录暂时没有翻开，请重试。' }
  finally { if (id === requestId) busy.value = false }
}
function toggle(event: Event) { opened.value = (event.target as HTMLDetailsElement).open; if (opened.value) load() }
</script>

<template>
  <details class="collection-records" @toggle="toggle">
    <summary>掉落统计与锻刀手记 <small>按地图整理掉落，一炉一条记下锻刀</small></summary>
    <div v-if="opened">
      <div class="collection-controls">
        <button :aria-pressed="kind === 'drops'" @click="kind = 'drops'; load()">掉落统计</button>
        <button :aria-pressed="kind === 'forge'" @click="kind = 'forge'; load()">锻刀手记</button>
        <label>记录范围 <select v-model="days" @change="load()"><option :value="7">最近 7 天</option><option :value="30">最近 30 天</option><option :value="0">全部</option></select></label>
        <button @click="load">刷新</button>
      </div>
      <p v-if="busy">正在整理……</p>
      <p v-else-if="error" role="alert">{{ error }}</p>
      <template v-else-if="kind === 'drops' && drops">
        <p class="record-note">只统计已采集的记录。战斗数按后端统计口径汇总，不等同周回数；旧记录可能不完整。</p>
        <p v-if="!drops.groups.length">这个范围还没有能归到地图的掉落统计。新记录会从采集后开始积累。</p>
        <article v-for="group in drops.groups" :key="group.key">
          <header><h4>{{ group.key }}</h4><span>统计战斗数 {{ fmt(group.battles) }} · 王点到达 {{ fmt(group.boss_reached) }} · 掉落 {{ group.drop_total }} 振</span></header>
          <ul><li v-for="(drop, index) in group.drops" :key="index"><b>{{ drop.name || '名字未识别' }}</b><span>{{ drop.count }} 振 <small v-if="drop.first_get_count">· 初入手 {{ drop.first_get_count }} 振</small></span></li></ul>
          <p v-if="!group.drops.length">尚无掉落明细。</p>
        </article>
        <p v-if="drops.unattributed.battles || drops.unattributed.drop_total">地图未确认的记录：{{ drops.unattributed.battles }} 场战斗、{{ drops.unattributed.drop_total }} 振掉落。</p>
      </template>
      <template v-else-if="kind === 'forge' && forge">
        <p class="record-note">配方顺序：木炭／玉钢／冷却材／砥石。近侍为开炉前最近一次登录观察；材料消耗按配方估算，符的实际支出见收支流水。</p>
        <p v-if="!forge.forges.length && !forge.orphan_collected.length">这个范围暂无锻刀记录。未采集到的旧炉无法补回。</p>
        <article v-for="(row, index) in visibleForges" :key="`${row.started_at}-${index}`">
          <header><h4>{{ date(row.started_at) }}</h4><span>{{ row.slot_no }} 号炉</span></header>
          <p>配方 {{ recipe(row.recipe) }} · 近侍 {{ row.secretary?.name || '未记录' }}<small v-if="row.secretary">（{{ date(row.secretary.observed_at) }} 观察）</small></p>
          <p>关联领取结果：<b>{{ row.swords?.map(s => `${s.name || '名字未识别'}${s.is_first_get_sword ? '（初入手）' : ''}`).join('、') || (row.collected_at == null ? '暂无领取记录' : '结果未记录') }}</b><span v-if="row.collected_at"> · {{ date(row.collected_at) }} 领取</span></p>
          <p v-if="row.cost_est">材料消耗估算 {{ recipe(row.cost_est) }}</p>
        </article>
        <button v-if="shown < forge.forges.length" @click="shown += 12">再看 12 炉（还有 {{ forge.forges.length - shown }} 炉）</button>
        <details v-if="forge.orphan_collected.length"><summary>只有领取记录的 {{ forge.orphan_collected.length }} 炉</summary><p v-for="(row, index) in forge.orphan_collected" :key="index">{{ date(row.collected_at) }} · {{ row.swords?.map(s => s.name || '名字未识别').join('、') || '结果未记录' }} · 开炉记录缺失</p></details>
      </template>
    </div>
  </details>
</template>

<style scoped>
.collection-records { background: var(--paper-card); border: 1px solid var(--paper-line); padding: 16px 20px; margin: 0 0 14px; }
summary { cursor: pointer; font-weight: 700; }
summary small { font-weight: 400; margin-left: 10px; }
small, .record-note { color: var(--ink-dim); }
.collection-controls, header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin: 12px 0; }
button { padding: 6px 12px; border: 1px solid var(--paper-line); background: var(--paper); cursor: pointer; }
button[aria-pressed="true"] { background: var(--gold-pale, #f5e8ba); color: var(--fox-gold-deep); }
article { padding: 8px 0; border-top: 1px dashed var(--paper-line); }
h4 { margin: 0; } p, li, header { font-size: 12px; line-height: 1.7; }
ul { list-style: none; margin: 0; padding: 0; }
li { display: flex; justify-content: space-between; gap: 12px; padding: 5px 0; }
@media(max-width: 600px) { .collection-records { padding: 12px; } summary small { display: block; margin: 5px 0; } }
</style>
