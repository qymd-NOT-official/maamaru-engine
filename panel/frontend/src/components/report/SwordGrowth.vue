<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../../api'
import type { TrainingHistoryResponse, TrainingOverviewSword, InternalAffairsSword, SwordJournalResponse, SwordJournalEntry, SwordJournalObtainedDetail } from '../../types'
const props = defineProps<{ serialId: number; server?: string }>()
const history = ref<TrainingHistoryResponse>()
const training = ref<TrainingOverviewSword>()
const affairs = ref<InternalAffairsSword>()
const journal = ref<SwordJournalResponse>()
const busy = ref(false), loaded = ref(false), error = ref('')
const labels = { obtained: '显现／入手记录', departed: '出发修行', returned: '修行归来', max_level_observed: '首次记录到 Lv.99' }
const fmt = (n: number | null | undefined) => n == null ? '未记录' : n.toLocaleString()
const date = (ts: number | null) => ts == null ? '日期未确认' : new Date(ts * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
function journalDetail(item: SwordJournalEntry) {
  if (item.kind !== 'obtained') return ''
  const detail = item.detail as SwordJournalObtainedDetail
  const sources: Record<string, string> = { forge: '锻刀', 'sortie.drop': '出阵掉落', 'battle.drop': '战斗掉落', 'raid.drop': '联队战掉落', 'inbox.claim': '收件箱领取' }
  return [detail.source ? sources[detail.source] || '来源待确认' : '', detail.chapter != null && detail.map_no != null ? `${detail.chapter}-${detail.map_no}` : '', detail.is_first_get_sword ? '初入手' : ''].filter(Boolean).join(' · ')
}
async function load(event: Event) {
  if (!(event.target as HTMLDetailsElement).open || loaded.value || busy.value) return
  busy.value = true
  error.value = ''
  const results = await Promise.allSettled([api.trainingHistory(props.serialId, props.server), api.trainingOverview(props.server), api.internalAffairs(props.server), api.swordJournal(props.serialId, props.server)])
  const [h, t, a, j] = results
  if (h.status === 'fulfilled') history.value = h.value ?? undefined
  if (t.status === 'fulfilled') training.value = t.value?.swords.find(s => s.serial_id === props.serialId)
  if (a.status === 'fulfilled') affairs.value = a.value?.swords.find(s => s.serial_id === props.serialId)
  if (j.status === 'fulfilled') journal.value = j.value ?? undefined
  if (results.some(r => r.status === 'rejected')) error.value = '部分记录没有翻开，请重试。'
  loaded.value = !error.value
  busy.value = false
}
</script>

<template>
  <details class="sword-growth" @toggle="load">
    <summary>成长与履历</summary>
    <p v-if="busy">正在翻记录……</p>
    <p v-if="error" role="alert">{{ error }} 收起后再打开即可重试。</p>
    <template v-if="!busy">
      <h4 v-if="journal?.timeline.length">入手履历</h4>
      <ol v-if="journal?.timeline.length"><li v-for="(item, index) in journal.timeline" :key="index"><time>{{ date(item.ts) }}</time><span>{{ labels[item.kind] }}<small v-if="journalDetail(item)"> · {{ journalDetail(item) }}</small></span></li></ol>
      <div class="growth-facts">
        <p v-if="training">截至 {{ training.captured_at }} · 累计经验 <b>{{ fmt(training.exp) }}</b> · 乱舞习合值 <b>{{ fmt(training.ranbu_exp) }}</b></p>
        <p v-if="training?.ranbu_next">下一级还差 {{ fmt(training.ranbu_next.need_exp) }} 习合值，约需 <b>{{ training.ranbu_next.need_swords_est }} 振同名刀（估算）</b>。</p>
        <p v-if="affairs">内番已养成：生存 {{ affairs.hp_up == null ? '未记录' : `+${fmt(affairs.hp_up)}` }} · 侦察 {{ affairs.scout_up == null ? '未记录' : `+${fmt(affairs.scout_up)}` }}<br><small v-if="affairs.hp_plateau || affairs.scout_plateau">{{ affairs.hp_plateau ? '生存' : '' }}{{ affairs.hp_plateau && affairs.scout_plateau ? '、' : '' }}{{ affairs.scout_plateau ? '侦察' : '' }}连续多次记录未增长，是否喂满尚未确认。</small></p>
      </div>
      <h4 v-if="history?.timeline.length">成长记录</h4>
      <ol v-if="history?.timeline.length">
        <li v-for="point in [...history.timeline].reverse()" :key="point.ts"><time>{{ point.captured_at }}</time><span>Lv.{{ point.level ?? '—' }} · 经验 {{ fmt(point.exp) }} · 乱舞 Lv.{{ point.ranbu_level ?? '—' }} · 习合值 {{ fmt(point.ranbu_exp) }}</span></li>
      </ol>
      <p v-if="loaded && !training && !affairs && !history?.timeline.length && !journal?.timeline.length">暂无记录。</p>
    </template>
  </details>
</template>

<style scoped>
.sword-growth { grid-column: 1 / -1; min-width: 0; width: 100%; border-top: 1px dashed var(--paper-line); padding-top: 8px; font-size: 12px; }
summary { cursor: pointer; color: var(--fox-gold-deep); }
p { line-height: 1.7; margin: 8px 0; }
small, time { color: var(--ink-dim); }
h4 { margin: 14px 0 6px; }
ol { list-style: none; padding: 0; margin: 0; }
li { display: flex; flex-wrap: wrap; gap: 6px 16px; padding: 7px 0; border-bottom: 1px solid var(--paper-line); }
time { font-variant-numeric: tabular-nums; }
</style>
