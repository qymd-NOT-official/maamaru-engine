<script setup lang="ts">
import { computed } from 'vue'
import type { HonmaruSituation } from '../types'
import { remainingTime } from './homeClockModel'

const props = defineProps<{ situation: HonmaruSituation | null; now: number }>()
const groups = computed(() => {
  const s = props.situation
  return [
    { name: '远征', observed: s?.parties_observed_at, rows: s?.parties.map(p => ({ label: `部队${p.party_no}`, finish: p.finished_at })) ?? [] },
    { name: '锻刀', observed: s?.forge_observed_at, rows: s?.forge_slots.map(p => ({ label: `${p.slot_no}号炉`, finish: p.finished_at })) ?? [] },
    { name: '手入', observed: s?.repair_observed_at, rows: s?.repair?.map(p => ({ label: `${p.slot_no}号手入室`, finish: p.finished_at })) ?? [] },
    { name: '内番', observed: s?.duty_observed_at, rows: s?.duty ? [{ label: '内番', finish: s.duty.finished_at }] : [] },
  ]
})
</script>

<template>
  <details class="honmaru-status">
    <summary>本丸状态</summary>
    <p>展示最近读取的记录；预计完成不代表已经领取。</p>
    <section v-for="group in groups" :key="group.name">
      <h3>{{ group.name }}</h3>
      <small>{{ group.observed ? `读取于 ${group.observed}` : '未读取' }}</small>
      <template v-if="group.observed">
        <div v-for="row in group.rows" :key="row.label"><span>{{ row.label }}</span><strong>{{ row.finish ? remainingTime(row.finish, now) : '未记录倒计时' }}</strong></div>
        <p v-if="!group.rows.length">已读取记录中没有倒计时。</p>
      </template>
    </section>
  </details>
</template>

<style scoped>
.honmaru-status { margin-top: 12px; border-top: 1px solid var(--paper-line); padding-top: 12px; }
summary { cursor: pointer; color: var(--home-green); }
section { padding-top: 12px; }
h3 { font-size: 13px; margin: 0; }
small, p { color: var(--ink-dim); font-size: 11px; overflow-wrap: anywhere; }
div { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; padding-top: 8px; font-size: 12px; }
strong { font-weight: 500; }
</style>
