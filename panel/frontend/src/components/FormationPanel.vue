<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import type { CustomFormation, CustomFormationSlotEntry, FormationCandidate, HonmaruFormationProfile } from '../types'
import {
  candidateEvidenceGaps,
  candidateFormLabel,
  candidateName,
  presetCandidatePickable,
  presetSlotCount,
  presetSlotSummary,
  validatePresetDraft,
} from '../formation'

// 这里只管理「想套用什么队」的预设，不复刻游戏当前五队，也不把历史
// 点名冒充实时编队。真正套用预设由出阵/远征/任务流在开工前统一调用。

const props = withDefaults(defineProps<{
  server?: string
  running?: boolean
  current?: string | null
  stopping?: boolean
  starting?: boolean
}>(), { running: false, current: null, stopping: false, starting: false })
const emit = defineEmits<{ stop: []; runInventory: []; notify: [message: string] }>()

const TEAM_LABELS = ['部队一', '部队二', '部队三', '部队四', '部队五']
const MAX_PRESETS = 5 // 后端合同：预设编队最多存 5 套
const TREASURE_OPTIONS = [
  { label: '曜变天目', value: '曜变天目' },
  { label: '狮子螺钿鞍', value: '狮子螺钿鞍' },
  { label: '南蛮胴具足', value: '南蛮胴具足' },
  { label: '鍔·月下梅树透图', value: '锷·月下梅树透图' },
  { label: '鍔·双鹤', value: '锷·双鹤' },
  { label: '三所物·菊', value: '三所物·菊' },
  { label: '三所物·狮子', value: '三所物·狮子' },
] as const
const TROOP_SPECIAL = [
  { label: '轻步兵（新春）', value: '轻步兵·新春' },
  { label: '轻骑兵（新春）', value: '轻骑兵·新春' },
] as const
const TROOP_KINDS = [
  '轻步兵', '投石兵', '枪兵', '重步兵', '盾兵', '轻骑兵',
  '重骑兵', '精锐兵', '弓兵', '铳兵', '水炮兵',
] as const
const TROOP_GRADES = ['中', '上', '特上'] as const
const HORSE_OPTIONS = [
  { label: '王庭', value: '01王庭' }, { label: '三国黑', value: '02三国黑' },
  { label: '松风', value: '03松风' }, { label: '小云雀', value: '04小云雀' },
  { label: '高楯黑', value: '05高楯黑' }, { label: '花柑子', value: '06花柑子' },
  { label: '青海波', value: '07青海波' }, { label: '望月', value: '08望月' },
  ...['白毛', '鹿毛', '青毛'].map(name => ({ label: name, value: name })),
  ...['一', '二', '三', '四', '五', '六', '七', '八', '九'].map(no => ({ label: `祝${no}号`, value: `祝${no}号` })),
]
const CHARM_OPTIONS = ['御守', '御守·极', '御守·桃'] as const

const clientInventory = ref<Awaited<ReturnType<typeof api.clientInventory>> | null>(null)
const profile = ref<HonmaruFormationProfile | null>(null)
const loading = ref(true)
const loadError = ref('')

const pool = computed(() => profile.value?.candidate_pool || null)
const poolDone = computed(() => Boolean(pool.value?.done))
const entries = computed(() => poolDone.value ? pool.value?.entries || [] : [])

interface CandidateGroup { name: string; rows: FormationCandidate[] }

const candidateGroups = computed<CandidateGroup[]>(() => {
  const grouped = new Map<string, FormationCandidate[]>()
  for (const entry of entries.value) {
    const key = candidateName(entry)
    const rows = grouped.get(key) || []
    rows.push(entry)
    grouped.set(key, rows)
  }
  return [...grouped]
    .map(([name, rows]) => ({ name, rows }))
    .sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
})



async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [profileResult, inventoryResult, archiveResult] = await Promise.allSettled([
      api.honmaruProfile(props.server), api.clientInventory(props.server), api.swordArchive(props.server),
    ])
    if (archiveResult.status === 'fulfilled') archive.value = archiveResult.value
    if (inventoryResult.status === 'fulfilled') clientInventory.value = inventoryResult.value
    if (profileResult.status === 'fulfilled') profile.value = profileResult.value
    else loadError.value = '刀帐暂时没有翻开，请更新刀帐后再选刀。'
    if (archiveResult.status === 'rejected') loadError.value = '刀帐标签暂时没有翻开，请重新读取。'
  } catch (cause) {
    loadError.value = cause instanceof Error ? cause.message : '本丸档案没有翻开'
  } finally {
    loading.value = false
  }
}

// 预设编队管理：增删改只动记录、不碰游戏；真正应用时由玩法入口把关。
const presets = ref<CustomFormation[]>([])
const presetsLoading = ref(false)
const presetsError = ref('')
const editorOpen = ref(false)
const editingId = ref('') // '' = 新建
const draftName = ref('')
const draftTeam = ref(1)
const draftSlots = ref<Record<string, CustomFormationSlotEntry>>({})
const pickerSlot = ref<number | null>(null) // 正在选刀的格子
const equipmentSlot = ref<number | null>(null)
const pickerTag = ref<'favorite' | 'watch' | 'keeper' | 'all'>('all')
const archive = ref<Awaited<ReturnType<typeof api.swordArchive>> | null>(null)
const pickerTags = [{ value: 'favorite', label: '常用' }, { value: 'watch', label: '特别关心' }, { value: 'keeper', label: '要练' }, { value: 'all', label: '整本' }] as const
function candidateMarks(entry: FormationCandidate) {
  return archive.value?.entries.find(row => row.observation_id === entry.observation_id)?.human
}
const presetQuery = ref('')
const presetSaving = ref(false)
const presetMessage = ref('')
const presetFailed = ref(false)
const savedDraft = ref('')
const draftFingerprint = computed(() => JSON.stringify({ name: draftName.value, team: draftTeam.value, slots: draftSlots.value }))
const draftDirty = computed(() => editorOpen.value && savedDraft.value !== draftFingerprint.value)
const pendingAction = ref<'switch' | 'close' | ''>('')
const pendingPreset = ref<CustomFormation | undefined>()

function discardDraft() {
  const action = pendingAction.value
  const preset = pendingPreset.value
  pendingAction.value = ''
  savedDraft.value = draftFingerprint.value
  if (action === 'switch') openPresetEditor(preset)
  else if (action === 'close') closePresetEditor()
}

const draftSlotCount = computed(() => presetSlotCount({ slots: draftSlots.value }))
const draftError = computed(() => validatePresetDraft({
  name: draftName.value,
  target_team: draftTeam.value,
  slots: draftSlots.value,
}))

// 预设选刀池直接复用本丸档案的候选分组（同名多振逐振列出，同一口径）。
const presetFilteredGroups = computed(() => {
  const needle = presetQuery.value.trim()
  return candidateGroups.value.map(group => ({ ...group, rows: group.rows.filter(entry =>
    pickerTag.value === 'all' || candidateMarks(entry)?.[pickerTag.value])
  })).filter(group => group.rows.length && (!needle || group.name.includes(needle)))
})

function cloneSlots(slots: Record<string, CustomFormationSlotEntry>): Record<string, CustomFormationSlotEntry> {
  return JSON.parse(JSON.stringify(slots || {})) as Record<string, CustomFormationSlotEntry>
}

async function loadPresets() {
  presetsLoading.value = true
  presetsError.value = ''
  try {
    const body = await api.customFormations(props.server)
    presets.value = body.formations || []
    if (!editorOpen.value && presets.value.length) openPresetEditor(presets.value[0])
  } catch (cause) {
    presetsError.value = cause instanceof Error ? cause.message : '预设名单没有翻开'
  } finally {
    presetsLoading.value = false
  }
}

function openPresetEditor(preset?: CustomFormation) {
  if (presetSaving.value) return
  if (editorOpen.value && preset?.id === editingId.value) return
  if (draftDirty.value) {
    pendingPreset.value = preset
    pendingAction.value = 'switch'
    return
  }
  if (preset) {
    editingId.value = preset.id
    draftName.value = preset.name
    draftTeam.value = preset.target_team
    draftSlots.value = cloneSlots(preset.slots)
  } else {
    editingId.value = ''
    draftName.value = ''
    draftTeam.value = 1
    draftSlots.value = {}
  }
  pickerSlot.value = null
  equipmentSlot.value = null
  presetQuery.value = ''
  presetMessage.value = ''
  presetFailed.value = false
  editorOpen.value = true
  pendingAction.value = ''
  savedDraft.value = draftFingerprint.value
}

function closePresetEditor() {
  if (presetSaving.value) return
  if (draftDirty.value) {
    pendingAction.value = 'close'
    return
  }
  editorOpen.value = false
  pendingAction.value = ''
  pickerSlot.value = null
  equipmentSlot.value = null
}

function togglePresetPicker(no: number) {
  pickerSlot.value = pickerSlot.value === no ? null : no
  equipmentSlot.value = null
  presetQuery.value = ''
}

function toggleEquipmentEditor(no: number) {
  equipmentSlot.value = equipmentSlot.value === no ? null : no
  pickerSlot.value = null
}

function assignPresetCandidate(entry: FormationCandidate) {
  if (pickerSlot.value == null || !presetCandidatePickable(entry)) return
  const next = cloneSlots(draftSlots.value)
  next[String(pickerSlot.value)] = {
    ...(entry.observation_id ? { observation_id: entry.observation_id } : {}),
    ...(entry.sword_catalog_id ? { sword_catalog_id: entry.sword_catalog_id } : {}),
    ...(entry.same_team_exclusion_key ? { same_team_exclusion_key: entry.same_team_exclusion_key } : {}),
    ...(entry.name_zh ? { name_zh: entry.name_zh } : {}),
    ...(entry.level != null ? { level: entry.level } : {}),
    ...(entry.tou_level != null ? { tou_level: entry.tou_level } : {}),
    ...(entry.survival_max != null ? { survival_max: entry.survival_max } : {}),
    ...(entry.stats ? { stats: { ...entry.stats } } : {}),
    ...(entry.form_status ? { form_status: entry.form_status } : {}),
    ...(entry.kiwame_date ? { kiwame_date: entry.kiwame_date } : {}),
    ...(entry.source_snapshot_id != null ? { source_snapshot_id: entry.source_snapshot_id } : {}),
    ...(entry.observed_at != null ? { observed_at: entry.observed_at } : {}),
    ...(next[String(pickerSlot.value)]?.treasure ? { treasure: next[String(pickerSlot.value)].treasure } : {}),
    ...(next[String(pickerSlot.value)]?.troops ? { troops: next[String(pickerSlot.value)].troops } : {}),
    ...(next[String(pickerSlot.value)]?.horse ? { horse: next[String(pickerSlot.value)].horse } : {}),
    ...(next[String(pickerSlot.value)]?.charm ? { charm: next[String(pickerSlot.value)].charm } : {}),
  }
  draftSlots.value = next
  presetMessage.value = ''
  // 选完自动滑到下一个空位，一口气能把六个格子点完
  const rest = [1, 2, 3, 4, 5, 6].find(no => no !== pickerSlot.value && !next[String(no)])
  pickerSlot.value = rest ?? null
}

function clearPresetSlot(no: number) {
  const next = cloneSlots(draftSlots.value)
  delete next[String(no)]
  draftSlots.value = next
  if (equipmentSlot.value === no) equipmentSlot.value = null
}

function setSlotTroop(no: number, position: number, value: string) {
  const next = cloneSlots(draftSlots.value)
  const entry = next[String(no)]
  if (!entry) return
  const troops = { ...(entry.troops || {}) }
  if (value.trim()) troops[String(position)] = value.trim()
  else delete troops[String(position)]
  if (Object.keys(troops).length) entry.troops = troops
  else delete entry.troops
  draftSlots.value = next
}

function troopKind(name: string) {
  return name.replace(/·(中|上|特上)$/, '')
}

function troopGrade(name: string) {
  return name.match(/·(中|上|特上)$/)?.[1] || '特上'
}

function selectSlotTroopKind(no: number, position: number, kind: string) {
  const current = draftSlots.value[String(no)]?.troops?.[String(position)] || ''
  setSlotTroop(no, position, !kind || !TROOP_KINDS.some(item => item === kind)
    ? kind : `${kind}·${troopGrade(current)}`)
}

function selectSlotTroopGrade(no: number, position: number, grade: string) {
  const current = draftSlots.value[String(no)]?.troops?.[String(position)] || ''
  const kind = troopKind(current)
  if (kind && TROOP_KINDS.some(item => item === kind)) {
    setSlotTroop(no, position, `${kind}·${grade}`)
  }
}

function setSlotTreasure(no: number, field: 'name' | 'level' | 'affection', value: string) {
  const next = cloneSlots(draftSlots.value)
  const entry = next[String(no)]
  if (!entry) return
  const treasure = entry.treasure || { name: '', level: 1, affection: 0 }
  entry.treasure = { ...treasure, [field]: field === 'name' ? value : Number(value) }
  draftSlots.value = next
}

function selectSlotTreasure(no: number, name: string) {
  if (!name) clearSlotTreasure(no)
  else setSlotTreasure(no, 'name', name)
}

function setSlotAccessory(no: number, field: 'horse' | 'charm', value: string) {
  const next = cloneSlots(draftSlots.value)
  const entry = next[String(no)]
  if (!entry) return
  if (value) entry[field] = value
  else delete entry[field]
  draftSlots.value = next
}

function clearSlotTreasure(no: number) {
  const next = cloneSlots(draftSlots.value)
  delete next[String(no)]?.treasure
  draftSlots.value = next
}

async function savePreset() {
  if (presetSaving.value) return
  const problem = validatePresetDraft({
    name: draftName.value,
    target_team: draftTeam.value,
    slots: draftSlots.value,
  })
  if (problem) {
    presetMessage.value = problem
    presetFailed.value = true
    return
  }
  presetSaving.value = true
  presetMessage.value = ''
  try {
    const body = await api.saveCustomFormation(
      { name: draftName.value.trim(), target_team: draftTeam.value, slots: draftSlots.value },
      editingId.value || undefined, props.server,
    )
    if (!body.ok) throw new Error('没有保存成功，请重试')
    emit('notify', `预设「${body.formation.name}」已收好`)
    editingId.value = body.formation.id
    pendingAction.value = ''
    savedDraft.value = draftFingerprint.value
    pickerSlot.value = null
    equipmentSlot.value = null
    presetMessage.value = '已保存'
    await loadPresets()
  } catch (cause) {
    presetMessage.value = cause instanceof Error ? cause.message : '保存失败，请重试'
    presetFailed.value = true
  } finally {
    presetSaving.value = false
  }
}

async function removePreset(preset: CustomFormation) {
  if (presetSaving.value) return
  if (!window.confirm(`删除「${preset.name}」？这套预设将从名单里移除，无法恢复。`)) return
  try {
    const body = await api.deleteCustomFormation(preset.id, props.server)
    if (!body.ok) throw new Error('没有删除成功，请重试')
    presets.value = presets.value.filter(item => item.id !== preset.id)
    if (editingId.value === preset.id) {
      editorOpen.value = false
      pendingAction.value = ''
      if (presets.value.length) openPresetEditor(presets.value[0])
    }
    emit('notify', `已删除预设「${preset.name}」`)
  } catch (cause) {
    emit('notify', cause instanceof Error ? cause.message : '删除失败，请重试')
  }
}

onMounted(() => { load(); loadPresets() })
</script>

<template>
  <section class="formation-panel">
    <section class="formation-workspace">

      <p v-if="loadError" class="formation-error">{{ loadError }}</p>
      <div v-if="loading && !profile" class="formation-empty">正在读取刀剑名册……</div>
      <template v-else>
        <section class="formation-presets">
          <aside class="formation-preset-sidebar" aria-label="我的部队预设">
          <header class="formation-presets-head">
            <div>
              <h3>我的部队预设</h3>
            </div>
            <button
              type="button"
              class="secondary"
              :disabled="presetsLoading || presets.length >= MAX_PRESETS"
              :title="presets.length >= MAX_PRESETS ? '最多存 5 套预设，先删掉一套不用的' : ''"
              @click="openPresetEditor()"
            >{{ presets.length >= MAX_PRESETS ? '最多 5 套' : '＋ 新建预设' }}</button>
          </header>

          <p v-if="presetsError" class="formation-error" role="alert">{{ presetsError }}</p>
          <p v-else-if="presetsLoading && !presets.length" class="formation-empty">正在翻预设名单……</p>
          <template v-else>
            <p v-if="!presets.length" class="formation-empty">{{ props.server === 'jp' ? '还没有预设编队。先存下常用阵容，整队套用待适配。' : '还没有预设编队。把常用的阵容存下来，下次整套换上，不用一格一格点。' }}</p>
            <ul v-else class="formation-preset-list">
              <li v-for="preset in presets" :key="preset.id" class="formation-preset-card" :class="{ selected: editorOpen && editingId === preset.id }">
                <button type="button" class="formation-preset-info" :aria-pressed="editorOpen && editingId === preset.id" :disabled="presetSaving" @click="openPresetEditor(preset)">
                  <b>{{ preset.name }}</b>
                  <span class="formation-badges">
                    <i>{{ TEAM_LABELS[preset.target_team - 1] }} · {{ presetSlotCount(preset) }}/6 位</i>
                  </span>
                </button>
                <div class="formation-preset-tools">
                  <button type="button" class="danger" :aria-label="`删除预设「${preset.name}」`" :disabled="presetSaving" @click="removePreset(preset)">删除</button>
                </div>
              </li>
            </ul>
            <p v-if="presets.length >= MAX_PRESETS" class="formation-hintline">最多存 5 套预设；想存新的，先删掉一套不用的。</p>
          </template>
          </aside>
          <section class="formation-preset-detail" aria-label="调整部队预设">
          <div v-if="pendingAction" class="formation-preset-warn" role="alertdialog" aria-label="未保存的预设修改">
            <p>这套预设还有修改没保存。要放下修改吗？</p>
            <div class="formation-discard-actions"><button type="button" class="secondary" @click="pendingAction = ''">继续编辑</button><button type="button" class="secondary" @click="discardDraft">放下修改</button></div>
          </div>
          <div v-if="editorOpen" class="formation-preset-editor">
            <h4>{{ editingId ? '编辑预设' : '新建预设' }}</h4>
            <div class="formation-preset-form">
              <label class="formation-preset-field">
                <span>预设名字</span>
                <input v-model="draftName" type="text" maxlength="20" placeholder="比如：演练主力队">
              </label>
              <label class="formation-preset-field">
                <span>覆盖部队</span>
                <select v-model.number="draftTeam">
                  <option v-for="(label, index) in TEAM_LABELS" :key="label" :value="index + 1">{{ label }}</option>
                </select>
              </label>
            </div>
            <p class="formation-hintline">{{ props.server === 'jp' ? '从日服刀帐选择具体一振；刀装和宝物仅记录计划。保存不会调整游戏里的部队。' : '从刀帐选择具体一振。选好后点该位置的「设置装备」，填写刀装和宝物；留空的位置应用时保持原样。' }}</p>
            <p v-if="draftSlotCount === 0" class="formation-preset-warn">{{ props.server === 'jp' ? '也可以先存一份空预设，之后再补人。' : '一个位置都没指定也行，存是能存，但应用时没有可做的事，会直接停下。' }}</p>
            <ol class="formation-preset-slots">
              <li v-for="no in [1, 2, 3, 4, 5, 6]" :key="no">
                <button
                  type="button"
                  class="formation-preset-slot"
                  :class="{ active: pickerSlot === no, filled: Boolean(draftSlots[String(no)]) }"
                  :aria-pressed="pickerSlot === no"
                  @click="togglePresetPicker(no)"
                >
                  <b>{{ no }}号位</b>
                  <span>{{ presetSlotSummary(draftSlots[String(no)]) }}</span>
                </button>
                <button
                  v-if="draftSlots[String(no)]"
                  type="button"
                  class="formation-preset-clear"
                  :aria-label="`清除 ${no} 号位，恢复成不动`"
                  title="清除，恢复成不动"
                  @click="clearPresetSlot(no)"
                >×</button>
                <button
                  v-if="draftSlots[String(no)]"
                  type="button"
                  class="formation-equipment-open secondary"
                  :aria-expanded="equipmentSlot === no"
                  @click="toggleEquipmentEditor(no)"
                >{{ equipmentSlot === no ? '收起装备' : '设置装备（刀装／马／御守／宝物）' }}</button>
              </li>
            </ol>

            <div v-if="equipmentSlot != null && draftSlots[String(equipmentSlot)]" class="formation-treasure-editor">
              <b>{{ equipmentSlot }}号位的装备</b>
              <p>每格先选种类，再选品级；新春刀装不用选品级。留空的格子保持原样；这振刀没有该格或仓库找不到时会停止应用。</p>
              <div class="formation-preset-form">
                <div v-for="position in [1, 2, 3]" :key="position" class="formation-preset-field">
                  <span>刀装第 {{ position }} 格</span>
                  <div class="formation-troop-selects">
                    <select :aria-label="`刀装第 ${position} 格种类`" :value="troopKind(draftSlots[String(equipmentSlot)].troops?.[String(position)] || '')" @change="selectSlotTroopKind(equipmentSlot!, position, ($event.target as HTMLSelectElement).value)">
                      <option value="">不指定</option>
                      <option v-if="draftSlots[String(equipmentSlot)].troops?.[String(position)] && !TROOP_KINDS.some(kind => kind === troopKind(draftSlots[String(equipmentSlot)].troops?.[String(position)] || '')) && !TROOP_SPECIAL.some(item => item.value === troopKind(draftSlots[String(equipmentSlot)].troops?.[String(position)] || ''))" :value="troopKind(draftSlots[String(equipmentSlot)].troops?.[String(position)] || '')">{{ draftSlots[String(equipmentSlot)].troops?.[String(position)] }}</option>
                      <optgroup label="活动刀装">
                        <option v-for="item in TROOP_SPECIAL" :key="item.value" :value="item.value">{{ item.label }}</option>
                      </optgroup>
                      <optgroup label="普通刀装">
                        <option v-for="kind in TROOP_KINDS" :key="kind" :value="kind">{{ kind }}</option>
                      </optgroup>
                    </select>
                    <select v-if="TROOP_KINDS.some(kind => kind === troopKind(draftSlots[String(equipmentSlot)].troops?.[String(position)] || ''))" :aria-label="`刀装第 ${position} 格品级`" :value="troopGrade(draftSlots[String(equipmentSlot)].troops?.[String(position)] || '')" @change="selectSlotTroopGrade(equipmentSlot!, position, ($event.target as HTMLSelectElement).value)">
                      <option v-for="grade in TROOP_GRADES" :key="grade" :value="grade">{{ grade }}</option>
                    </select>
                  </div>
                </div>
              </div>
              <div class="formation-preset-form formation-accessory-form">
                <label class="formation-preset-field">
                  <span>马匹</span>
                  <select :value="draftSlots[String(equipmentSlot)].horse || ''" @change="setSlotAccessory(equipmentSlot!, 'horse', ($event.target as HTMLSelectElement).value)">
                    <option value="">不指定</option>
                    <option v-if="draftSlots[String(equipmentSlot)].horse && !HORSE_OPTIONS.some(option => option.value === draftSlots[String(equipmentSlot)].horse)" :value="draftSlots[String(equipmentSlot)].horse">{{ draftSlots[String(equipmentSlot)].horse }}</option>
                    <option v-for="option in HORSE_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
                  </select>
                </label>
                <label class="formation-preset-field">
                  <span>御守</span>
                  <select :value="draftSlots[String(equipmentSlot)].charm || ''" @change="setSlotAccessory(equipmentSlot!, 'charm', ($event.target as HTMLSelectElement).value)">
                    <option value="">不指定</option>
                    <option v-if="draftSlots[String(equipmentSlot)].charm && !CHARM_OPTIONS.some(option => option === draftSlots[String(equipmentSlot)].charm)" :value="draftSlots[String(equipmentSlot)].charm">{{ draftSlots[String(equipmentSlot)].charm }}</option>
                    <option v-for="option in CHARM_OPTIONS" :key="option" :value="option">{{ option }}{{ clientInventory?.items?.[option] ? ` · 所持 ${clientInventory.items[option].count}` : '' }}</option>
                  </select>
                </label>
              </div>
              <b class="formation-equipment-subtitle">宝物</b>
              <small v-if="clientInventory?.assets?.artifact">最近读到 {{ clientInventory.assets.artifact.length }} 件宝物</small>
              <p>选填。按游戏里的名称、等级、爱用度填写；同样信息的宝物有多件时会停下，避免选错。</p>
              <div class="formation-preset-form">
                <label class="formation-preset-field">
                  <span>宝物名称</span>
                  <select :value="draftSlots[String(equipmentSlot)].treasure?.name || ''" @change="selectSlotTreasure(equipmentSlot!, ($event.target as HTMLSelectElement).value)">
                    <option value="">不指定</option>
                    <option v-if="draftSlots[String(equipmentSlot)].treasure?.name && !TREASURE_OPTIONS.some(option => option.value === draftSlots[String(equipmentSlot)].treasure?.name)" :value="draftSlots[String(equipmentSlot)].treasure?.name">{{ draftSlots[String(equipmentSlot)].treasure?.name }}</option>
                    <option v-for="option in TREASURE_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
                  </select>
                </label>
                <label class="formation-preset-field">
                  <span>等级</span>
                  <input :value="draftSlots[String(equipmentSlot)].treasure?.level ?? 1" type="number" min="1" @input="setSlotTreasure(equipmentSlot!, 'level', ($event.target as HTMLInputElement).value)">
                </label>
                <label class="formation-preset-field">
                  <span>爱用度</span>
                  <input :value="draftSlots[String(equipmentSlot)].treasure?.affection ?? 0" type="number" min="0" @input="setSlotTreasure(equipmentSlot!, 'affection', ($event.target as HTMLInputElement).value)">
                </label>
              </div>
              <button v-if="draftSlots[String(equipmentSlot)].treasure" type="button" class="secondary" @click="clearSlotTreasure(equipmentSlot!)">不指定宝物</button>
            </div>

            <div v-if="pickerSlot != null" class="formation-preset-picker">
              <div class="formation-picker-modes" role="group" aria-label="刀帐标签">
                <button v-for="tag in pickerTags" :key="tag.value" type="button" class="secondary" :aria-pressed="pickerTag === tag.value" @click="pickerTag = tag.value">{{ tag.label }}</button>
              </div>
              <label class="formation-search">
                <span>给 {{ pickerSlot }} 号位选刀</span>
                <input v-model="presetQuery" type="search" placeholder="输入刀名">
                <em>{{ presetFilteredGroups.reduce((sum, group) => sum + group.rows.length, 0) }} 振</em>
              </label>
              <p v-if="!poolDone" class="formation-empty">请先更新刀帐，再选择具体一振。</p>
              <div v-else-if="presetFilteredGroups.length" class="formation-preset-candidates">
                <section v-for="group in presetFilteredGroups" :key="group.name" class="formation-candidate-group">
                  <h4 v-if="group.rows.length > 1"><b>{{ group.name }}</b><small>同名 {{ group.rows.length }} 振，按档案逐振选</small></h4>
                  <button
                    v-for="(entry, index) in group.rows"
                    :key="entry.observation_id"
                    type="button"
                    class="formation-candidate"
                    :class="{ 'lacks-evidence': !presetCandidatePickable(entry) }"
                    :disabled="!presetCandidatePickable(entry)"
                    :title="!presetCandidatePickable(entry) ? '档案里没认出这振的名字，先重新跑一次「刀帐盘点」再来' : ''"
                    @click="assignPresetCandidate(entry)"
                  >
                    <b>{{ group.rows.length > 1 ? `第 ${index + 1} 振` : group.name }}</b>
                    <span class="formation-badges">
                      <i :class="{ kiwame: entry.form_status === 'kiwame' }" :title="(entry.form_evidence || []).join('；')">{{ candidateFormLabel(entry) }}</i>
                      <i>Lv.{{ entry.level ?? '—' }}</i>
                      <i v-for="tag in pickerTags.filter(tag => tag.value !== 'all' && candidateMarks(entry)?.[tag.value])" :key="tag.value">{{ tag.label }}</i>
                      <i v-for="gap in candidateEvidenceGaps(entry)" :key="gap" class="formation-gap">缺{{ gap }}</i>
                    </span>
                  </button>
                </section>
              </div>
              <p v-else class="formation-empty">这个范围里没有符合条件的刀。</p>
            </div>

            <p v-if="presetMessage" class="formation-preset-message" :class="{ failed: presetFailed }" :role="presetFailed ? 'alert' : 'status'">{{ presetMessage }}</p>
            <div class="formation-preset-actions">
              <span class="formation-save-state">{{ draftDirty ? '有修改尚未保存' : editingId ? '✓ 已保存' : '新预设' }}</span>
              <button type="button" class="primary" :disabled="presetSaving || Boolean(draftError)" @click="savePreset">{{ presetSaving ? '正在收好……' : '保存预设' }}</button>
              <button type="button" class="secondary" :disabled="presetSaving" @click="closePresetEditor">取消</button>
            </div>
            <p v-if="draftError" class="formation-hintline">{{ draftError }}</p>
          </div>
          <div v-else class="formation-detail-empty"><h3>安排一套常用阵容</h3><p>从左边选一套预设，或新建一套，再在这里调整刀剑和装备。</p></div>
          </section>
        </section>
      </template>
    </section>
  </section>
</template>

<style scoped>
/* 只管布局与编队特有零件；颜色、按钮、卡片全走全局样式与现有组件。 */
.formation-panel { min-width: 0; }
.formation-workspace { overflow: hidden; }
.formation-notice { margin: 12px 18px 16px; padding: 10px 13px; color: #9f3d28; background: color-mix(in srgb, #f4dfd7 68%, var(--paper-card)); border: 1px solid #d8a195; border-radius: 8px; font-size: 12px; }
.formation-hintline { margin: 8px 18px 14px; color: var(--ink-dim); font-size: 12px; }
.formation-treasure-editor { margin: 12px 18px; padding: 12px; border: 1px solid var(--paper-line); border-radius: 9px; }
.formation-treasure-editor p { margin: 5px 0 10px; color: var(--ink-dim); font-size: 12px; }
.formation-treasure-editor .secondary { margin-top: 9px; }
.formation-equipment-subtitle { display: block; margin-top: 14px; }
.formation-accessory-form { margin-top: 14px; }
.formation-search { display: grid; grid-template-columns: auto minmax(120px, 1fr) auto; align-items: center; gap: 9px; margin-bottom: 9px; color: var(--ink-dim); font-size: 12px; }
.formation-search input { width: 100%; min-width: 0; padding: 8px 10px; border: 1px solid var(--paper-line); border-radius: 8px; }
.formation-search em { font-style: normal; white-space: nowrap; }
.formation-candidate-group { border: 1px solid var(--paper-line); border-radius: 9px; overflow: hidden; }
.formation-candidate-group h4 { display: flex; align-items: baseline; gap: 8px; margin: 0; padding: 7px 11px; background: color-mix(in srgb, var(--paper) 55%, var(--paper-card)); border-bottom: 1px solid var(--paper-line); font-size: 12px; }
.formation-candidate-group h4 small { color: var(--ink-dim); font-size: 10px; font-weight: 400; }
.formation-candidate { display: flex; align-items: center; justify-content: space-between; gap: 8px; width: 100%; padding: 9px 11px; text-align: left; background: var(--paper); border: 0; border-top: 1px solid var(--paper-line); cursor: pointer; font-size: 12px; }
.formation-candidate:first-of-type { border-top: 0; }
.formation-candidate.active { background: var(--fox-gold-pale); box-shadow: inset 3px 0 0 var(--fox-gold); }
.formation-candidate:disabled { cursor: default; }
.formation-badges { display: flex; flex-wrap: wrap; gap: 4px; justify-content: flex-end; }
.formation-badges i { padding: 2px 6px; font-size: 10px; font-style: normal; background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 999px; }
.formation-badges i.kiwame { color: #8a5a18; border-color: color-mix(in srgb, var(--fox-gold) 65%, var(--paper-line)); }
.formation-badges .formation-gap { color: #9f3d28; border-color: #d8a195; }
.formation-error, .formation-empty { display: grid; gap: 3px; margin: 16px 18px; padding: 18px; color: var(--ink-dim); background: var(--paper-card); border: 1px dashed var(--paper-line); border-radius: 10px; font-size: 13px; }
.formation-error { color: #9f3d28; }
/* 预设编队管理区：列表 + 就地展开的编辑器，视觉零件与选人区同源。 */
.formation-presets { display: grid; grid-template-columns: 286px minmax(0, 1fr); min-height: 560px; }
.formation-preset-sidebar { padding: 22px 16px; background: color-mix(in srgb, var(--paper) 80%, var(--fox-gold-pale)); border-right: 1px solid var(--paper-line); min-width: 0; }
.formation-preset-detail { padding: 38px; min-width: 0; }
.formation-detail-empty { padding: 60px 20px; text-align: center; color: var(--ink-dim); }
.formation-detail-empty h3 { color: var(--ink); }
.formation-preset-sidebar .formation-empty { margin: 16px 0; padding: 12px; }
.formation-presets-head { display: grid; gap: 20px; }
.formation-presets-head h3 { margin: 0; font-size: 14px; }
.formation-presets-head p { max-width: 560px; margin: 4px 0 0; color: var(--ink-dim); font-size: 12px; line-height: 1.6; }
.formation-presets-head button { width: 100%; min-height: 44px; padding: 9px 14px; text-align: left; background: transparent; border-style: dashed; font-size: 14px; }
.formation-preset-list { display: grid; gap: 8px; margin: 12px 0 0; padding: 0; list-style: none; }
.formation-preset-card { display: flex; align-items: center; gap: 4px; padding: 4px; border: 1px solid transparent; border-radius: 9px; }
.formation-preset-card.selected { background: var(--paper-card); border-color: var(--paper-line); box-shadow: inset 3px 0 var(--fox-gold); }
.formation-preset-info { display: grid; gap: 7px; min-width: 0; flex: 1; text-align: left; padding: 12px 8px; color: var(--ink); border: 0; background: transparent; cursor: pointer; }
.formation-preset-info:focus-visible { outline: 2px solid var(--fox-gold); outline-offset: 2px; }
.formation-preset-info b { font-size: 13px; overflow-wrap: anywhere; }
.formation-preset-info .formation-badges { justify-content: flex-start; }
.formation-preset-tools { display: flex; flex: 0 0 auto; gap: 6px; }
.formation-preset-tools button { min-height: 30px; padding: 5px 6px; font-size: 11px; color: var(--ink-dim); background: transparent; border-color: transparent; box-shadow: none; }
.formation-preset-tools button:hover { color: #8f3524; border-color: var(--paper-line); }
.formation-preset-editor { padding: 24px; background: var(--paper-card); border: 1px solid var(--paper-line); box-shadow: 6px 6px 0 var(--paper-line); }
.formation-preset-editor h4 { margin: 0 0 20px; padding-bottom: 14px; border-bottom: 1px solid var(--paper-line); font-size: 20px; }
.formation-preset-editor .formation-hintline { margin: 8px 0 0; }
.formation-presets > .formation-hintline { margin: 10px 0 0; }
.formation-preset-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
.formation-preset-field { display: grid; gap: 5px; color: var(--ink-dim); font-size: 12px; }
.formation-preset-field input, .formation-preset-field select { width: 100%; min-width: 0; padding: 8px 10px; color: var(--ink); background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 8px; }
.formation-troop-selects { display: grid; grid-template-columns: minmax(0, 1fr) 68px; gap: 6px; }
.formation-troop-selects select:only-child { grid-column: 1 / -1; }
.formation-preset-warn { margin: 10px 0 0; padding: 8px 11px; color: #7a5312; background: color-mix(in srgb, #f4e8cf 72%, var(--paper-card)); border: 1px solid #d9bd84; border-radius: 8px; font-size: 12px; }
.formation-preset-slots { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 12px 0 0; padding: 0; list-style: none; }
.formation-preset-slots li { position: relative; min-width: 0; }
.formation-preset-slot { display: grid; gap: 3px; width: 100%; min-height: 54px; padding: 9px 28px 9px 11px; text-align: left; background: var(--paper); border: 1px solid var(--paper-line); border-radius: 9px; cursor: pointer; }
.formation-preset-slot b { font-size: 11px; color: var(--ink-dim); }
.formation-preset-slot span { font-size: 12px; overflow-wrap: anywhere; }
.formation-preset-slot.filled { background: color-mix(in srgb, var(--fox-gold-pale) 45%, var(--paper)); }
.formation-preset-slot.active { border-color: var(--fox-gold); box-shadow: 3px 3px 0 color-mix(in srgb, var(--paper-line) 60%, transparent); }
.formation-equipment-open { width: 100%; margin-top: 5px; min-height: 30px; padding: 5px 8px; font-size: 11px; }
.formation-preset-clear { position: absolute; top: 6px; right: 6px; display: grid; place-items: center; width: 20px; height: 20px; padding: 0; color: var(--ink-dim); background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 50%; font-size: 12px; line-height: 1; cursor: pointer; }
.formation-preset-clear:hover { color: #8f3524; border-color: #d8a195; }
.formation-preset-picker { margin-top: 10px; }
.formation-picker-modes { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.formation-picker-modes button { min-height: 30px; padding: 5px 10px; font-size: 12px; }
.formation-picker-modes button[aria-pressed="true"] { border-color: var(--fox-gold); background: var(--fox-gold-pale); }
.formation-preset-candidates { display: grid; gap: 8px; max-height: 300px; margin-top: 8px; overflow: auto; }
.formation-ranked-choice { display: grid; grid-template-columns: minmax(0, 1fr) repeat(2, minmax(0, auto)); align-items: center; }
.formation-ranked-choice > b { min-width: 0; padding: 8px 10px; font-size: 12px; overflow-wrap: anywhere; }
.formation-ranked-choice .formation-candidate { width: auto; height: 100%; border-top: 0; border-left: 1px solid var(--paper-line); white-space: nowrap; }
.formation-preset-message { margin: 10px 0 0; font-size: 12px; color: #2f5527; }
.formation-preset-message.failed { color: #8f3524; }
.formation-preset-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 22px; padding-top: 16px; border-top: 1px solid var(--paper-line); }
.formation-save-state { flex: 1; color: var(--ink-dim); font-size: 12px; }
.formation-discard-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.formation-preset-actions button { min-height: 34px; padding: 6px 18px; font-size: 12px; }
@media (max-width: 900px) {
  .formation-workspace { display: flex; flex-direction: column; overflow: visible; }
  .formation-presets { grid-template-columns: 1fr; }
  .formation-preset-sidebar { border-right: 0; border-bottom: 1px solid var(--paper-line); }
  .formation-preset-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .formation-preset-detail { padding: 18px; }
}
@media (max-width: 620px) {
  .formation-preset-detail { padding: 14px; }
  .formation-preset-editor { padding: 16px; }
  .formation-preset-list { grid-template-columns: 1fr; }
  .formation-candidate { align-items: flex-start; flex-direction: column; gap: 4px; }
  .formation-badges { justify-content: flex-start; }
  .formation-preset-card { align-items: flex-start; flex-direction: column; }
  .formation-preset-tools { width: 100%; }
  .formation-preset-tools button { flex: 1; }
  .formation-preset-slots { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .formation-ranked-choice { grid-template-columns: 1fr 1fr; }
  .formation-ranked-choice > b { grid-column: 1 / -1; }
  .formation-ranked-choice .formation-candidate { width: 100%; border-top: 1px solid var(--paper-line); }
}
@media (prefers-reduced-motion: reduce) {
  .formation-candidate, .formation-preset-slot { transition: none; }
}
</style>
