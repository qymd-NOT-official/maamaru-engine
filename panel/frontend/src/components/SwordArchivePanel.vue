<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import PaperCard from './PaperCard.vue'
import PanelHeader from './PanelHeader.vue'
import SwordGrowth from './report/SwordGrowth.vue'
import SwordReadActions from './SwordReadActions.vue'
import PixelControl from './PixelControl.vue'
import SegmentedControl from './SegmentedControl.vue'
import type { SwordAnnotationBody, SwordArchiveAttentionItem, SwordArchiveEntry, SwordArchiveResponse } from '../types'
import {
  ARCHIVE_ALL_TYPES,
  SWORD_TYPE_OPTIONS,
  archiveFormLabel,
  archiveFormSource,
  archiveName,
  attentionReasonTexts,
  attentionTarget,
  duplicateOrdinals,
  favoriteBody,
  filterArchiveEntries,
  formConfirmBody,
  groupAttentionItems,
  keeperBody,
  levelConfirmBody,
  parseLevelInput,
  partitionWatchEntries,
  sortArchiveEntries,
  watchBody,
} from '../archive'

// 刀帐档案：先按「待核对 / 特别关心 / 要练 / 常用 / 整本」选择要看的范围。
// 整本行默认只读，改判形态、三种标记与撤销收进单行「整理」；待核对仍直接给操作。
// 编号/筛选/分组/置顶/请求体全在 archive.ts，这里只做展示和递请求。
// entry.human?.watch 全程点属性访问，别解构——和 vue 的 watch API 撞名。

const props = defineProps<{
  running: boolean
  current: string | null
  stopping: boolean
  starting: boolean
}>()
const emit = defineEmits<{ runInventory: [] }>()

const data = ref<SwordArchiveResponse | null>(null)
const loading = ref(true)
const error = ref('')
const saving = ref(false)
const query = ref('')
const swordType = ref<string>(ARCHIVE_ALL_TYPES)
const archiveView = ref<'attention' | 'watch' | 'keeper' | 'favorite' | 'all'>('attention')
const expandedEntryId = ref<string | null>(null)
// 每条「等级没读出来」的等级草稿，按 attentionKey 各自独立绑定，互不串行
const levelDrafts = ref<Record<string, string>>({})

const done = computed(() => Boolean(data.value?.done))
const summary = computed(() => data.value?.summary || null)
const entries = computed(() => data.value?.entries || [])
const attention = computed(() => data.value?.attention || [])

const sortedEntries = computed(() => sortArchiveEntries(entries.value))
const ordinals = computed(() => duplicateOrdinals(entries.value))
const markedCounts = computed(() => ({
  watch: entries.value.filter(entry => entry.human?.watch).length,
  keeper: entries.value.filter(entry => entry.human?.keeper).length,
  favorite: entries.value.filter(entry => entry.human?.favorite).length,
}))
const viewItems = computed(() => [
  { value: 'attention', label: '待核对', badge: attention.value.length },
  { value: 'watch', label: '特别关心', badge: markedCounts.value.watch },
  { value: 'keeper', label: '要练', badge: markedCounts.value.keeper },
  { value: 'favorite', label: '常用', badge: markedCounts.value.favorite },
  { value: 'all', label: '整本', badge: entries.value.length },
])
const viewTitle = computed(() => {
  if (archiveView.value === 'watch') return '特别关心'
  if (archiveView.value === 'keeper') return '要练的刀'
  if (archiveView.value === 'favorite') return '常用刀'
  return '整本刀帐'
})
const visibleEntries = computed(() => {
  const filtered = filterArchiveEntries(sortedEntries.value, query.value, swordType.value)
  if (archiveView.value === 'watch') return filtered.filter(entry => entry.human?.watch)
  if (archiveView.value === 'keeper') return filtered.filter(entry => entry.human?.keeper)
  if (archiveView.value === 'favorite') return filtered.filter(entry => entry.human?.favorite)
  return filtered
})

// 特别关心置顶：搜完筛完再切，特别关心的一组顶在整本前面，中间画分隔线
const watchSplit = computed(() => partitionWatchEntries(visibleEntries.value))
const displayEntries = computed(() => [...watchSplit.value.watched, ...watchSplit.value.rest])
const watchedCount = computed(() => watchSplit.value.watched.length)
const attentionGroups = computed(() => groupAttentionItems(attention.value))

// 行的形态来源徽标：算一次按 observation_id 查，别在模板里反复调
interface RowSource { kind: 'machine' | 'human' | 'overridden'; text: string; origin: string | null }
function rowSourceOf(entry: SwordArchiveEntry): RowSource {
  const source = archiveFormSource(entry)
  if (source.kind === 'machine') return { kind: 'machine', text: entry.data_source === 'youzu_log' ? '游戏记录' : '盘点识别', origin: null }
  if (source.kind === 'human') return { kind: 'human', text: '你确认过', origin: null }
  return { kind: 'overridden', text: '你改判的', origin: `原识别：${source.machineText}` }
}
const sourceById = computed(() => {
  const map = new Map<string, RowSource>()
  for (const entry of entries.value) map.set(entry.observation_id, rowSourceOf(entry))
  return map
})

const typeItems = computed(() => [
  { value: ARCHIVE_ALL_TYPES, label: ARCHIVE_ALL_TYPES, badge: entries.value.length },
  ...SWORD_TYPE_OPTIONS.map(type => ({
    value: type as string,
    label: type,
    badge: entries.value.filter(entry => entry.sword_type === type).length,
  })),
])

const overviewSubtitle = computed(() => {
  if (!data.value) return '整本刀帐 + 你亲手记下的标注'
  const parts = [`档案时间 ${data.value.observed_at ? fmtTime(data.value.observed_at) : '—'}`]
  if (data.value.snapshot_id != null) parts.push(`第 ${data.value.snapshot_id} 号盘点`)
  if (data.value.data_source === 'youzu_log') parts.push('游戏所持名单')
  return parts.join(' · ')
})

function fmtTime(value: number) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    timeZone: 'Asia/Shanghai',
  }).format(new Date(value * 1000))
}

function entryOf(observationId: string | null): SwordArchiveEntry | null {
  if (observationId == null) return null
  return entries.value.find(entry => entry.observation_id === observationId) || null
}

// stale 条目没有 observation_id（标注对不上任何行），key 用指纹兜底
function attentionKey(item: SwordArchiveAttentionItem): string {
  return item.observation_id || `stale:${item.sword_catalog_id || '?'}:${item.kiwame_date || '?'}`
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.swordArchive()
    if (archiveView.value === 'attention' && !data.value.attention.length) archiveView.value = 'all'
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '刀帐档案没有翻开'
  } finally {
    loading.value = false
  }
}

function toggleEntryEditor(entry: SwordArchiveEntry) {
  expandedEntryId.value = expandedEntryId.value === entry.observation_id ? null : entry.observation_id
}

async function annotate(body: SwordAnnotationBody): Promise<boolean> {
  if (saving.value) return false
  saving.value = true
  error.value = ''
  try {
    await api.saveSwordAnnotation(body)
    await load()
    return true
  } catch (cause) {
    error.value = cause instanceof Error ? `这次没能记下：${cause.message}` : '这次没能记下，请重试'
    return false
  } finally {
    saving.value = false
  }
}

// ---- 整本刀帐行操作：改判形态 / 三个标记开关 / 撤销标注 ----

// 每行都能改判：只翻 form 一位，旧标注其余字段由 formConfirmBody 递回
function confirmEntryForm(entry: SwordArchiveEntry, form: 'kiwame' | 'normal') {
  annotate(formConfirmBody(entry, form, entry.human))
}

function toggleKeeper(entry: SwordArchiveEntry) {
  annotate(keeperBody(entry, !(entry.human?.keeper ?? false), entry.human))
}
function toggleFavorite(entry: SwordArchiveEntry) {
  annotate(favoriteBody(entry, !(entry.human?.favorite ?? false), entry.human))
}
function toggleWatch(entry: SwordArchiveEntry) {
  annotate(watchBody(entry, !(entry.human?.watch ?? false), entry.human))
}

// 撤销：软删整条人工标注，这振回到机器盘点的识别结果，三种标记也一起放下
async function revokeEntry(entry: SwordArchiveEntry) {
  const human = entry.human
  if (!human || saving.value) return
  if (!window.confirm(`撤销对「${archiveName(entry)}」的亲手标注吗？\n撤销后这振会回到机器盘点的识别结果，常用/特别关心/要练的标记也会一起放下。`)) return
  saving.value = true
  error.value = ''
  try {
    await api.revokeSwordAnnotation(human.id)
    await load()
  } catch (cause) {
    error.value = cause instanceof Error ? `这次没能撤销：${cause.message}` : '这次没能撤销，请重试'
  } finally {
    saving.value = false
  }
}

function viewSameName(item: SwordArchiveAttentionItem) {
  query.value = item.name_zh || ''
  swordType.value = ARCHIVE_ALL_TYPES
  archiveView.value = 'all'
}

async function revokeOldAnnotation(item: SwordArchiveAttentionItem) {
  if (!item.annotation_id || saving.value) return
  if (!window.confirm(`撤销「${item.name_zh || '这振刀'}」${item.kiwame_date || ''} 的旧标注吗？\n只撤销本地标记，不影响游戏里的刀剑。`)) return
  saving.value = true
  error.value = ''
  try {
    await api.revokeSwordAnnotation(item.annotation_id)
    await load()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '旧标注未能撤销'
  } finally {
    saving.value = false
  }
}

// 待核对区：是极 / 是普通 / 是要练的刀。旧标注从同 observation_id
// 的档案行里找回来，改一位、其余原样带回。
function confirmAttention(item: SwordArchiveAttentionItem, form: 'kiwame' | 'normal') {
  annotate(formConfirmBody(attentionTarget(item), form, entryOf(item.observation_id)?.human))
}
function keepAttention(item: SwordArchiveAttentionItem) {
  annotate(keeperBody(attentionTarget(item), true, entryOf(item.observation_id)?.human))
}

// 等级没读出来的条目：填 1~99 的整数才给递，记下成功就清掉这行的草稿
async function confirmLevel(item: SwordArchiveAttentionItem) {
  const key = attentionKey(item)
  const parsed = parseLevelInput(levelDrafts.value[key] || '')
  const body = parsed == null
    ? null
    : levelConfirmBody(attentionTarget(item), parsed, entryOf(item.observation_id)?.human)
  if (!body) {
    error.value = '等级要填 1～99 的整数，才能记下。'
    return
  }
  if (await annotate(body)) {
    const drafts = { ...levelDrafts.value }
    delete drafts[key]
    levelDrafts.value = drafts
  }
}

onMounted(load)
</script>

<template>
  <section class="archive-panel">
    <PaperCard variant="task" tag="section" class="archive-heading">
      <PanelHeader title="刀帐档案" :subtitle="overviewSubtitle" variant="embedded">
        <template #actions>
          <SwordReadActions :running="running" :current="current" :stopping="stopping" :starting="starting"
            @updated="load" @error="error = $event" @run-inventory="emit('runInventory')" />
        </template>
      </PanelHeader>
      <div v-if="summary" class="archive-summary">
        <div><small>共</small><b>{{ summary.total }} 振</b></div>
        <div><small>你确认过</small><b>{{ summary.human_confirmed }} 振</b></div>
        <div><small>要练的刀</small><b>{{ summary.keepers }} 振</b></div>
        <div><small>待核对</small><b>{{ summary.attention_count }} 条</b></div>
      </div>
      <p v-if="!done && data" class="archive-notice">
        {{ data.reason || '还没有所持刀剑名单' }}。进入本丸后点“更新刀帐”。
      </p>
    </PaperCard>

    <p v-if="error" class="archive-error" role="alert">{{ error }}</p>
    <div v-else-if="loading && !data" class="archive-empty">正在翻刀帐……</div>

    <template v-else-if="data">
      <SegmentedControl v-model="archiveView" class="archive-view-switch" :items="viewItems" label="查看刀帐" />

      <PaperCard v-if="archiveView !== 'attention'" variant="task" tag="section" class="archive-book">
        <h3 class="archive-sub">{{ viewTitle }} · {{ visibleEntries.length }} 振</h3>
        <div class="archive-toolbar">
          <PixelControl v-model="query" type="search" placeholder="输入刀名找一找" aria-label="搜索刀名" />
          <em>{{ visibleEntries.length }} 振</em>
        </div>
        <SegmentedControl v-model="swordType" :items="typeItems" label="按刀种筛选" class="archive-type-switch" />
        <ul v-if="displayEntries.length" class="archive-list">
          <template v-for="(entry, index) in displayEntries" :key="entry.observation_id">
            <li v-if="archiveView === 'all' && index === watchedCount && watchedCount > 0" class="archive-watch-divider" aria-hidden="true"><span>整本刀帐</span></li>
            <li class="archive-entry" :class="{ editing: expandedEntryId === entry.observation_id }">
              <div class="archive-entry-name">
                <b>{{ archiveName(entry) }}</b>
                <i v-if="entry.human?.favorite" class="archive-mark favorite">常用</i>
                <i v-if="entry.human?.watch" class="archive-mark watch">特别关心</i>
                <i v-if="entry.human?.keeper" class="archive-mark keeper">要练</i>

                <small v-if="ordinals.get(entry.observation_id)">第 {{ ordinals.get(entry.observation_id) }} 振</small>
                <i v-if="entry.human?.stale" class="archive-stale">待复核</i>
              </div>
              <button type="button" class="archive-edit-toggle" :aria-expanded="expandedEntryId === entry.observation_id" @click="toggleEntryEditor(entry)">{{ expandedEntryId === entry.observation_id ? '收好' : '整理' }}</button>
              <div class="archive-entry-facts">
                <i class="archive-form" :class="entry.form_status" :title="(entry.form_evidence || []).join('；')">{{ archiveFormLabel(entry) }}</i>
                <span class="archive-number"><small>等级</small><b>Lv.{{ entry.level ?? '—' }}</b><i v-if="entry.human?.level != null" class="archive-confirmed archive-level-tag" title="你填写的等级">你填的</i></span>
                <span class="archive-number"><small>累计经验</small><b>{{ entry.exp == null ? '—' : entry.exp.toLocaleString() }}</b></span>
                <span class="archive-number"><small>乱舞</small><b>Lv.{{ entry.tou_level ?? '—' }}</b><small v-if="entry.ranbu_exp != null">{{ entry.ranbu_exp.toLocaleString() }} 习合值</small></span>
                <span class="archive-birthday"><small>显现</small>{{ entry.kiwame_date || '—' }}</span>
              </div>
              <div v-if="expandedEntryId === entry.observation_id" class="archive-entry-actions">
                <i class="archive-source" :class="sourceById.get(entry.observation_id)?.kind">{{ sourceById.get(entry.observation_id)?.text }}</i>
                <small v-if="sourceById.get(entry.observation_id)?.origin" class="archive-source-origin">{{ sourceById.get(entry.observation_id)?.origin }}</small>
                <i v-for="hint in entry.hints" :key="hint" class="archive-hint">{{ hint }}</i>
                <span v-if="entry.acquisition" :title="entry.acquisition.origin_message">获得：{{ entry.acquisition.label }}{{ entry.acquisition.location ? ` · ${entry.acquisition.location}` : '' }}{{ entry.acquisition.mailbox_id ? ' · 收件箱领取' : '' }}</span>
                <span v-if="entry.data_source === 'youzu_log'">生存 {{ entry.survival ?? '—' }}/{{ entry.survival_max ?? '—' }} · 疲劳 {{ entry.fatigue ?? '—' }} · {{ entry.locked == null ? '保护状态未知' : entry.locked ? '已保护' : '未保护' }}</span>
                <span class="archive-form-confirm" role="group" aria-label="改判形态">
                  <button type="button" class="secondary" :disabled="saving" @click="confirmEntryForm(entry, 'kiwame')">是极</button>
                  <button type="button" class="secondary" :disabled="saving" @click="confirmEntryForm(entry, 'normal')">是普通</button>
                </span>
                <button type="button" class="archive-pill favorite" :class="{ active: entry.human?.favorite }" :aria-pressed="Boolean(entry.human?.favorite)" :disabled="saving" title="顺手就要用的刀，再点取消" @click="toggleFavorite(entry)">{{ entry.human?.favorite ? '常用 ✓' : '常用' }}</button>
                <button type="button" class="archive-pill watch" :class="{ active: entry.human?.watch }" :aria-pressed="Boolean(entry.human?.watch)" :disabled="saving" title="置顶特别盯着，再点取消" @click="toggleWatch(entry)">{{ entry.human?.watch ? '特别关心 ✓' : '特别关心' }}</button>
                <button type="button" class="archive-pill keeper" :class="{ active: entry.human?.keeper }" :aria-pressed="Boolean(entry.human?.keeper)" :disabled="saving" title="点了就是要练的刀，再点取消" @click="toggleKeeper(entry)">{{ entry.human?.keeper ? '要练 ✓' : '要练' }}</button>
                <button v-if="entry.human" type="button" class="archive-revoke" :disabled="saving" title="撤销亲手标注，回到机器盘点的识别结果" @click="revokeEntry(entry)">撤销</button>
              </div>
              <SwordGrowth v-if="entry.serial_id" :key="entry.serial_id" :serial-id="entry.serial_id" />
              <small v-if="entry.human?.stale" class="archive-stale-note">同名同日有多振，标记挂在这一组上，不保证选中具体哪一振</small>
            </li>
          </template>
        </ul>
        <p v-else class="archive-clean">没有找到这样的刀。</p>
      </PaperCard>

      <PaperCard v-else variant="task" tag="section" class="archive-attention">
        <h3 class="archive-sub">待核对 · {{ attention.length }} 条</h3>
        <p v-if="!attention.length" class="archive-clean">现在没有要核对的条目，刀帐清清爽爽。</p>
        <template v-else>
          <div v-for="group in attentionGroups" :key="group.reason" class="archive-attention-group">
            <h4 class="archive-group-title">{{ group.title }} · {{ group.items.length }} 条</h4>
            <p v-if="group.reason === 'stale_annotation'" class="archive-sub">旧标注的日期与当前名单未匹配，暂未套用。</p>
            <ul class="archive-attention-list">
              <li v-for="item in group.items" :key="attentionKey(item)" class="archive-attention-row">
                <div class="archive-row-head">
                  <b>{{ item.name_zh || '没认出名字' }}</b>
                  <small v-if="item.observation_id && ordinals.get(item.observation_id)">第 {{ ordinals.get(item.observation_id) }} 振</small>
                  <span class="archive-facts">
                    <template v-if="item.level != null">Lv.{{ item.level }}</template>
                    <template v-if="item.kiwame_date"> · {{ item.reasons.includes('stale_annotation') ? '旧标注日期' : '显现' }} {{ item.kiwame_date }}</template>
                  </span>
                </div>
                <div class="archive-badges">
                  <i v-for="text in attentionReasonTexts(item.reasons)" :key="text" class="archive-reason">{{ text }}</i>
                  <i v-for="hint in item.hints" :key="hint" class="archive-hint">{{ hint }}</i>
                </div>
                <div v-if="item.reasons.includes('stale_annotation')" class="archive-actions">
                  <button type="button" class="secondary" @click="viewSameName(item)">查看同名刀</button>
                  <button v-if="item.annotation_id" type="button" class="secondary" :disabled="saving" @click="revokeOldAnnotation(item)">撤销旧标注</button>
                </div>
                <div v-else class="archive-actions">
                  <button type="button" class="secondary" :disabled="saving" @click="confirmAttention(item, 'kiwame')">是极</button>
                  <button type="button" class="secondary" :disabled="saving" @click="confirmAttention(item, 'normal')">是普通</button>
                  <button type="button" class="secondary" :disabled="saving" @click="keepAttention(item)">是要练的刀</button>
                </div>
                <div v-if="item.reasons.includes('level_unknown')" class="archive-level">
                  <PixelControl
                    v-model="levelDrafts[attentionKey(item)]"
                    type="number"
                    :min="1"
                    :max="99"
                    placeholder="等级"
                    aria-label="填等级（1 到 99）"
                    @keyup.enter="confirmLevel(item)"
                  />
                  <button
                    type="button"
                    class="secondary"
                    :disabled="saving || parseLevelInput(levelDrafts[attentionKey(item)] || '') == null"
                    @click="confirmLevel(item)"
                  >记下等级</button>
                  <small v-if="(levelDrafts[attentionKey(item)] || '').trim() && parseLevelInput(levelDrafts[attentionKey(item)] || '') == null" class="archive-level-bad">要填 1～99 的整数</small>
                </div>
              </li>
            </ul>
          </div>
        </template>
      </PaperCard>
      <details v-if="data?.historical_annotations?.length" class="archive-history">
        <summary>历史标注 · {{ data.historical_annotations.length }} 条</summary>
        <p class="archive-sub">已不在当前所持名单，保留原标注。</p>
        <ul class="archive-attention-list">
          <li v-for="item in data.historical_annotations" :key="item.annotation_id" class="archive-attention-row">
            <b>{{ item.name_zh }}</b>
            <span class="archive-facts"> · 显现 {{ item.kiwame_date }}</span>
            <small v-if="item.departure_reason">{{ item.departure_reason === '刀解' ? '已刀解' : `已用于${item.departure_reason}` }}</small>
          </li>
        </ul>
      </details>
    </template>
  </section>
</template>

<style scoped>
/* 一条中轴线：三块卡片铺满同一容器。全局 task-card 的 max-width 和
   margin:0 auto 都要关掉——网格里 auto 外边距会打断拉伸，卡片会缩成
   内容宽悬在中间（编队页"东一块西一块"就是这么来的）。 */
.archive-panel { display: grid; gap: 13px; align-content: start; }
.archive-panel :deep(.task-card) { max-width: none; margin: 0; }
.archive-history summary { cursor: pointer; color: var(--ink-dim); }
.archive-view-switch { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); width: 100%; padding: 0; }
.archive-view-switch :deep(button) { min-width: 0; }
.archive-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); }
.archive-summary > div { display: grid; gap: 3px; padding: 13px 16px; border-left: 1px solid var(--paper-line); }
.archive-summary > div:first-child { border-left: 0; }
.archive-summary small { color: var(--ink-dim); font-size: 10px; }
.archive-summary b { font-size: 18px; font-variant-numeric: tabular-nums; }
.archive-notice { margin: 12px 16px 14px; padding: 10px 13px; color: #9f3d28; background: color-mix(in srgb, #f4dfd7 68%, var(--paper-card)); border: 1px solid #d8a195; border-radius: 8px; font-size: 12px; }
.archive-error { margin: 0; padding: 12px 14px; color: #9f3d28; background: color-mix(in srgb, #f4dfd7 68%, var(--paper-card)); border: 1px solid #d8a195; border-radius: 9px; font-size: 12px; }
.archive-sub { margin: 2px 0 10px; color: var(--ink-dim); font-size: 12px; letter-spacing: .06em; }
.archive-clean { margin: 0; padding: 14px; color: var(--ink-dim); background: var(--paper); border: 1px dashed var(--paper-line); border-radius: 9px; font-size: 12px; text-align: center; }
.archive-empty { display: grid; gap: 3px; margin: 0; padding: 18px; color: var(--ink-dim); background: var(--paper-card); border: 1px dashed var(--paper-line); border-radius: 10px; font-size: 13px; }

/* 整本刀帐：每行 = 名字带来源徽标 / 操作组 / 事实行，操作组窄屏自动换行。
   特别关心置顶，和其余刀之间隔一条金色分隔线。 */
.archive-growth-note { color: var(--ink-dim); font-size: 12px; line-height: 1.7; }
.archive-toolbar { display: grid; grid-template-columns: minmax(170px, 330px) auto 1fr; align-items: center; gap: 10px; margin-bottom: 10px; color: var(--ink-dim); font-size: 12px; }
.archive-toolbar :deep(.pixel-control) { width: 100%; min-height: 36px; padding: 7px 10px; color: var(--ink); background: var(--paper); border: 1px solid var(--paper-line); border-radius: 8px; font: inherit; }
.archive-toolbar em { font-style: normal; white-space: nowrap; }
.archive-list { display: grid; gap: 7px; margin: 12px 0 0; padding: 0; list-style: none; }
/* 列表长就内部限高滚动，且全页只此一层内滚；窄屏取消内滚整页滚动。 */
@media (min-width: 901px) {
  .archive-list { max-height: 620px; overflow: auto; padding-right: 4px; }
}
.archive-watch-divider { display: flex; align-items: center; gap: 10px; margin: 4px 0 0; color: var(--fox-gold-deep); font-size: 11px; letter-spacing: .08em; }
.archive-watch-divider::before, .archive-watch-divider::after { content: ''; flex: 1; height: 1px; background: color-mix(in srgb, var(--fox-gold) 55%, var(--paper-line)); }
.archive-watch-divider span { white-space: nowrap; }
.archive-entry { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px 12px; align-items: center; padding: 9px 12px; background: var(--paper); border: 1px solid var(--paper-line); border-radius: 9px; font-size: 12px; }
.archive-entry-name { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px; min-width: 0; }
.archive-entry-name b { font-size: 13px; }
.archive-entry-name small { color: var(--fox-gold-deep); font-size: 10px; }
.archive-edit-toggle { min-height: 28px; padding: 3px 11px; color: var(--ink-dim); background: transparent; border: 1px solid var(--paper-line); border-radius: 999px; font-size: 11px; }
.archive-edit-toggle:hover, .archive-entry.editing .archive-edit-toggle { color: var(--fox-gold-deep); background: var(--fox-gold-pale); border-color: var(--fox-gold); }
.archive-source { padding: 1px 7px; border: 1px solid var(--paper-line); border-radius: 999px; font-size: 10px; font-style: normal; }
.archive-source.machine { color: var(--ink-dim); background: var(--paper-card); }
.archive-source.human { color: #426b36; background: color-mix(in srgb, #dcebd6 72%, var(--paper-card)); border-color: #b2caa8; }
.archive-source.overridden { color: #7a5312; background: color-mix(in srgb, #f4e8cf 75%, var(--paper-card)); border-color: #d9bd84; }
.archive-source-origin { color: var(--ink-dim); font-size: 10px; }
.archive-confirmed { padding: 1px 7px; color: #426b36; background: color-mix(in srgb, #dcebd6 72%, var(--paper-card)); border: 1px solid #b2caa8; border-radius: 999px; font-size: 10px; font-style: normal; }
.archive-level-tag { margin-left: 5px; }
.archive-stale { padding: 1px 7px; color: #9f3d28; background: color-mix(in srgb, #f4dfd7 70%, var(--paper-card)); border: 1px solid #d8a195; border-radius: 999px; font-size: 10px; font-style: normal; }
.archive-stale-note { grid-column: 1 / -1; color: #9f3d28; font-size: 10px; }
.archive-entry-actions { grid-column: 1 / -1; display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; padding-top: 7px; border-top: 1px dashed var(--paper-line); }
.archive-form-confirm { display: inline-flex; gap: 5px; }
.archive-entry-actions button { min-height: 28px; padding: 3px 11px; font-size: 11px; }
.archive-pill { color: var(--ink-dim); background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 999px; }
.archive-pill.favorite.active { color: #426b36; background: color-mix(in srgb, #dcebd6 72%, var(--paper-card)); border-color: #b2caa8; font-weight: 700; }
.archive-pill.watch.active { color: #9f3d28; background: color-mix(in srgb, #f4dfd7 70%, var(--paper-card)); border-color: #d8a195; font-weight: 700; }
.archive-pill.keeper.active { color: #75560b; background: var(--fox-gold-pale); border-color: var(--fox-gold); font-weight: 700; }
.archive-revoke { color: var(--ink-dim); background: transparent; border: 1px dashed var(--paper-line); border-radius: 999px; }
.archive-revoke:hover:not(:disabled) { color: #9f3d28; border-color: #d8a195; }
.archive-entry-facts { grid-column: 1 / -1; display: flex; flex-wrap: wrap; align-items: center; gap: 5px 10px; color: var(--ink-dim); font-variant-numeric: tabular-nums; }
.archive-form { padding: 2px 8px; color: var(--ink); background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 999px; font-size: 10px; font-style: normal; }
.archive-form.kiwame { color: #8a5a18; border-color: color-mix(in srgb, var(--fox-gold) 65%, var(--paper-line)); }
.archive-form.ambiguous { color: #9f3d28; border-color: #d8a195; }
.archive-mark { padding: 2px 8px; border: 1px solid var(--paper-line); border-radius: 999px; font-size: 10px; font-style: normal; font-weight: 700; }
.archive-mark.favorite { color: #426b36; background: color-mix(in srgb, #dcebd6 72%, var(--paper-card)); border-color: #b2caa8; }
.archive-mark.watch { color: #9f3d28; background: color-mix(in srgb, #f4dfd7 70%, var(--paper-card)); border-color: #d8a195; }
.archive-mark.keeper { color: #75560b; background: var(--fox-gold-pale); border-color: var(--fox-gold); }

/* 待核对：作为默认工作视图，按原因分组直接处理。 */
.archive-attention { border-left: 5px solid var(--fox-gold-deep); }
.archive-attention-group { display: grid; gap: 8px; margin-top: 10px; }
.archive-group-title { margin: 0; color: var(--ink-dim); font-size: 11px; font-weight: 600; letter-spacing: .05em; }
.archive-attention-list { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.archive-attention-row { display: grid; gap: 8px; padding: 11px 13px; background: var(--paper); border: 1px solid var(--paper-line); border-radius: 9px; }
.archive-row-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; font-size: 13px; }
.archive-row-head small { color: var(--fox-gold-deep); font-size: 11px; }
.archive-facts { color: var(--ink-dim); font-size: 11px; font-variant-numeric: tabular-nums; }
.archive-badges { display: flex; flex-wrap: wrap; gap: 5px; }
.archive-reason { padding: 2px 8px; color: #7a5312; background: color-mix(in srgb, #f4e8cf 75%, var(--paper-card)); border: 1px solid #d9bd84; border-radius: 999px; font-size: 10px; font-style: normal; }
.archive-hint { padding: 2px 8px; color: var(--ink-dim); background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 999px; font-size: 10px; font-style: normal; }
.archive-actions { display: flex; flex-wrap: wrap; gap: 7px; }
.archive-actions button { min-height: 30px; padding: 4px 13px; font-size: 12px; }
/* 等级填写照操作组的风格排：行内 flex 自然换行，不把行撑歪 */
.archive-level { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; }
.archive-level :deep(.pixel-control) { width: 96px; min-height: 30px; padding: 4px 9px; color: var(--ink); background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 8px; font: inherit; font-variant-numeric: tabular-nums; }
.archive-level button { min-height: 30px; padding: 4px 13px; font-size: 12px; }
.archive-level-bad { color: #9f3d28; font-size: 11px; }

@media (max-width: 900px) {
  .archive-view-switch { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .archive-view-switch :deep(button:last-child) { grid-column: 1 / -1; }
  .archive-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .archive-summary > div:nth-child(3) { border-left: 0; border-top: 1px solid var(--paper-line); }
  .archive-summary > div:nth-child(4) { border-top: 1px solid var(--paper-line); }
  .archive-toolbar { grid-template-columns: 1fr auto; }
  .archive-entry { grid-template-columns: 1fr; }
  .archive-entry-actions { justify-content: flex-start; }
}
@media (prefers-reduced-motion: reduce) {
  .archive-pill, .archive-revoke, .archive-actions button { transition: none; }
}

/* 刀帐按账页排版：筛选轻一些，数值沿固定列阅读。 */
.archive-heading :deep(.embedded-head) { padding: 0 0 10px; margin-bottom: 0; }
.archive-heading :deep(h2) { font-size: 21px; }
.archive-summary { display: flex; flex-wrap: wrap; gap: 8px 24px; padding-top: 10px; }
.archive-summary > div { display: flex; align-items: baseline; gap: 7px; padding: 0; border: 0; }
.archive-summary b { font-size: 15px; }
.archive-heading { padding: 18px 20px; }
.archive-type-switch { display: flex; flex-wrap: wrap; gap: 6px; border: 0; background: transparent; }
.archive-type-switch :deep(button) { flex: 0 0 auto; display: flex; align-items: center; gap: 7px; min-height: 30px; padding: 5px 10px; border: 1px solid var(--paper-line); border-radius: 4px; background: transparent; font-size: 12px; }
.archive-type-switch :deep(button.active) { background: var(--fox-gold-pale); border-color: var(--fox-gold); }
.archive-type-switch :deep(button em) { font-size: 10px; }
.archive-list { gap: 0; }
.archive-entry { padding: 14px 4px; border: 0; border-bottom: 1px solid var(--paper-line); border-radius: 0; background: transparent; gap: 9px 16px; }
.archive-entry-name b { font-size: 16px; }
.archive-entry-facts { display: grid; grid-template-columns: 48px 100px minmax(100px, 1fr) minmax(110px, 1fr) 140px; gap: 14px; align-items: center; }
.archive-number { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; }
.archive-number > small:first-child { width: 100%; font-size: 10px; }
.archive-number b { color: var(--ink); font-size: 15px; font-weight: 600; }
.archive-birthday { display: grid; gap: 5px; font-size: 12px; }
.archive-birthday small { font-size: 10px; }
.archive-entry-actions { justify-content: flex-start; }
@media (max-width: 900px) {
  .archive-summary > div:nth-child(3), .archive-summary > div:nth-child(4) { border: 0; }
  .archive-entry { grid-template-columns: minmax(0, 1fr) auto; }
  .archive-edit-toggle { padding: 3px 8px; }
  .archive-entry-facts { grid-template-columns: 40px repeat(3, minmax(0, 1fr)); gap: 8px; }
  .archive-number b { font-size: 13px; overflow-wrap: anywhere; }
  .archive-birthday { grid-column: 2 / -1; display: flex; gap: 8px; }
  .archive-entry-name b { font-size: 15px; }
}
</style>
