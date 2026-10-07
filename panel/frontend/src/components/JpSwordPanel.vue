<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import PanelHeader from './PanelHeader.vue'
import type { TrainingOverviewResponse, TrainingOverviewSword } from '../types'

// 日服刀帐：日服练度快照（training.captured）只读总览。
// 不连游戏、不出自动化入口；数据由 /api/data/training/overview?server=jp
// 走日服独立账房库，与国服刀帐档案完全分开。

const data = ref<TrainingOverviewResponse | null>(null)
const loading = ref(true)
const error = ref('')
const query = ref('')

function swordName(sword: TrainingOverviewSword): string {
  return sword.name || (sword.sword_id ? `刀帐${sword.sword_id}` : `编号${sword.serial_id}`)
}

const filtered = computed(() => {
  const swords = data.value?.swords || []
  const q = query.value.trim()
  if (!q) return swords
  return swords.filter(sword => swordName(sword).includes(q))
})

function fmtNumber(value: number | null): string {
  return value == null ? '—' : value.toLocaleString()
}

function fmtRanbuNext(sword: TrainingOverviewSword): string {
  const next = sword.ranbu_next
  if (!next) return sword.ranbu_level != null ? '已满级' : '—'
  if (next.need_exp <= 0) return '已满级'
  return `还差 ${next.need_exp.toLocaleString()} 习合值（约 ${next.need_swords_est} 振）`
}

onMounted(async () => {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.trainingOverview('jp')
  } catch (err) {
    error.value = err instanceof Error ? err.message : '日服刀帐没加载出来，请刷新重试'
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <section class="jp-sword-panel">
    <PanelHeader variant="page" title="日服刀帐" :subtitle="data ? `最近更新 ${data.captured_at || '—'} · 已读取 ${data.sword_count} 振` : '已同步的练度快照'" />
    <div class="jp-sword-content">
      <p v-if="error" class="jp-sword-error">{{ error }}</p>
      <p v-else-if="loading" class="jp-sword-empty">正在翻日服刀帐……</p>
      <div v-else-if="!data" class="jp-sword-empty-card">
        <h3>还没有日服刀帐快照</h3>
        <p>到仓库点「读取游戏家底」，在打开的日服浏览器里游玩后，这里会自动更新刀帐。</p>
      </div>
      <template v-else>
        <p v-if="data.roster_complete === false" class="jp-sword-empty">已恢复目前读到的刀剑记录；进入「结成」更新完整名单后，可确认当前所持数量。</p>
        <div class="jp-sword-toolbar">
          <input v-model="query" type="search" placeholder="输入刀名找一找" aria-label="按刀名筛选">
          <span>{{ filtered.length }} 振</span>
        </div>
        <div class="jp-sword-table-scroll" role="region" aria-label="刀帐名单" tabindex="0">
        <table class="jp-sword-table">
          <thead>
            <tr><th>刀名</th><th>等级</th><th>累计经验</th><th>乱舞</th><th>习合值</th><th>距下一级乱舞</th></tr>
          </thead>
          <tbody>
            <tr v-for="sword in filtered" :key="sword.serial_id">
              <td class="name">{{ swordName(sword) }}</td>
              <td>Lv.{{ sword.level ?? '—' }}</td>
              <td>{{ fmtNumber(sword.exp) }}</td>
              <td>{{ sword.ranbu_level != null ? `Lv.${sword.ranbu_level}` : '—' }}</td>
              <td>{{ fmtNumber(sword.ranbu_exp) }}</td>
              <td class="next">{{ fmtRanbuNext(sword) }}</td>
            </tr>
          </tbody>
        </table>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
.jp-sword-content {
  padding: 0 4px 24px;
}

.jp-sword-empty,
.jp-sword-error {
  padding: 32px 8px;
  text-align: center;
  color: var(--ink-soft, #8a7f6a);
}

.jp-sword-error {
  color: var(--danger, #b4452f);
}

.jp-sword-empty-card {
  margin: 16px 4px;
  padding: 28px 24px;
  border: 1px dashed var(--line, #d8cdb4);
  border-radius: 12px;
  text-align: center;
  color: var(--ink-soft, #8a7f6a);
}

.jp-sword-empty-card h3 {
  margin: 0 0 8px;
  color: var(--ink, #4a4132);
}

.jp-sword-empty-card p {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
}

.jp-sword-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 12px 4px;
}

.jp-sword-toolbar input {
  flex: 1;
  max-width: 320px;
  padding: 8px 12px;
  border: 1px solid var(--line, #d8cdb4);
  border-radius: 8px;
  background: var(--paper, #fbf7ec);
  color: inherit;
}

.jp-sword-toolbar span {
  color: var(--ink-soft, #8a7f6a);
  font-size: 13px;
}

.jp-sword-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}

.jp-sword-table-scroll {
  max-width: 100%;
  overflow-x: auto;
}

.jp-sword-table th,
.jp-sword-table td {
  padding: 9px 10px;
  text-align: left;
  border-bottom: 1px solid var(--line, #e4dcc6);
  white-space: nowrap;
}

.jp-sword-table th {
  color: var(--ink-soft, #8a7f6a);
  font-weight: 500;
  font-size: 13px;
}

.jp-sword-table td.name {
  font-weight: 600;
}

.jp-sword-table td.next {
  color: var(--ink-soft, #8a7f6a);
  font-size: 13px;
  white-space: normal;
}
</style>
