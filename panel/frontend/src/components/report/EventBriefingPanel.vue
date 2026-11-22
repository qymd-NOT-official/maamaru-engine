<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../../api'
import type { EventBriefingResponse } from '../../types'
import PanelHeader from '../PanelHeader.vue'
import {
  BATTLE_OPTIONS,
  BONUS_VERDICT_META,
  DAMAGE_OPTIONS,
  briefingRequestBody,
  defaultBriefingForm,
  fatigueLabel,
  formLabel,
  loadBriefingForm,
  saveBriefingForm,
  type EventBriefingFormData,
} from './eventBriefingModel'

const form = ref<EventBriefingFormData>(defaultBriefingForm())
const pick = ref('')
const swordOptions = ref<Array<{ name: string; type: string }>>([])
const swordOptionsFailed = ref(false)
const generating = ref(false)
const error = ref('')
const briefing = ref<EventBriefingResponse | null>(null)

onMounted(async () => {
  // 表单记忆只走 localStorage，不进后端 saved-settings 白名单
  form.value = loadBriefingForm()
  try {
    const data = await api.swords()
    swordOptions.value = (data.swords || [])
      .map(sword => ({ name: sword.name_zh || sword.name, type: sword.type }))
      .sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN'))
  } catch {
    swordOptionsFailed.value = true
  }
})

watch(form, value => saveBriefingForm(value), { deep: true })

const availableSwordOptions = computed(() =>
  swordOptions.value.filter(option => !form.value.bonusSwords.includes(option.name)))

function addBonusSword() {
  const name = pick.value
  if (!name || form.value.bonusSwords.includes(name)) return
  form.value.bonusSwords = [...form.value.bonusSwords, name]
  pick.value = ''
}

async function generate() {
  error.value = ''
  if (!form.value.name.trim()) {
    error.value = '活动得有个名字，参谋才知道给谁出主意。'
    return
  }
  generating.value = true
  try {
    briefing.value = await api.eventBriefing(
      briefingRequestBody(form.value), form.value.enemyFastSpear)
  } catch (cause) {
    briefing.value = null
    error.value = cause instanceof Error ? cause.message : '简报没生成出来，请重试'
  } finally {
    generating.value = false
  }
}
</script>

<template>
  <section class="briefing-panel">
    <PanelHeader variant="section" title="活动开幕参谋" subtitle="喂一期活动参数，对着刀帐出编队 / 刀装 / 控花简报" />

    <form class="briefing-form" @submit.prevent="generate">
      <div class="briefing-fields">
        <label>活动名
          <input v-model="form.name" maxlength="40" required placeholder="比如：战力扩充计划">
        </label>
        <label>伤害类型
          <select v-model="form.damage">
            <option v-for="option in DAMAGE_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
          </select>
        </label>
        <label>战场类型
          <select v-model="form.battle">
            <option v-for="option in BATTLE_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
          </select>
        </label>
        <label>地图点数
          <input v-model="form.mapPoints" inputmode="numeric" placeholder="不知道就空着（1~99）">
        </label>
        <label>建议等级
          <input v-model="form.recommendedLevel" inputmode="numeric" placeholder="不知道就空着（1~99）">
        </label>
        <label class="briefing-spear">
          <input v-model="form.enemyFastSpear" type="checkbox">
          <span>敌方有高速枪（短刀优先铳兵）</span>
        </label>
      </div>

      <div class="briefing-bonus">
        <label>加成刀（多选，名单来自刀剑图鉴）
          <select v-model="pick" :disabled="!availableSwordOptions.length" @change="addBonusSword">
            <option value="" disabled>{{ swordOptionsFailed ? '刀名清单没加载出来，刷新页签再试' : '选一把加进来' }}</option>
            <option v-for="option in availableSwordOptions" :key="option.name" :value="option.name">{{ option.name }}（{{ option.type }}）</option>
          </select>
        </label>
        <div v-if="form.bonusSwords.length" class="briefing-chips">
          <button
            v-for="name in form.bonusSwords"
            :key="name"
            type="button"
            class="briefing-chip"
            :title="`把 ${name} 从加成刀拿掉`"
            @click="form.bonusSwords = form.bonusSwords.filter(item => item !== name)"
          >{{ name }} ×</button>
        </div>
        <p v-else class="briefing-bonus-empty">没填加成刀也行，编队按战场类型走。</p>
      </div>

      <div class="briefing-actions">
        <button type="submit" class="primary" :disabled="generating">{{ generating ? '参谋翻阅刀帐中……' : '生成简报' }}</button>
      </div>
    </form>

    <p v-if="error" class="briefing-error" role="alert">{{ error }}</p>

    <div v-if="briefing" class="briefing-result">
      <p class="briefing-summary">{{ briefing.summary }}</p>

      <section v-if="briefing.warnings.length" class="briefing-warnings" aria-label="伤势与疲劳警示">
        <h4>⚠ 先处理这些，别硬上阵</h4>
        <ul>
          <li v-for="warning in briefing.warnings" :key="warning">{{ warning }}</li>
        </ul>
      </section>

      <section v-if="briefing.bonus.length" class="briefing-card" aria-label="加成刀评级">
        <h4>加成刀评级</h4>
        <ul class="briefing-bonus-list">
          <li v-for="item in briefing.bonus" :key="item.name" :class="['briefing-verdict', BONUS_VERDICT_META[item.verdict].tone]">
            <header>
              <strong>{{ item.name }}</strong>
              <em>{{ BONUS_VERDICT_META[item.verdict].label }}</em>
              <small v-if="item.level != null">{{ item.level }} 级<template v-if="item.form === 'kiwame'"> · 极</template></small>
            </header>
            <p>{{ item.reason }}</p>
          </li>
        </ul>
      </section>

      <section class="briefing-card" aria-label="编队候选">
        <h4>编队候选 <small>{{ briefing.formation.template_note }}</small></h4>
        <p class="briefing-wanted">优先刀种：{{ briefing.formation.wanted_types.join('、') }}</p>
        <ol v-if="briefing.formation.candidates.length" class="briefing-candidates">
          <li v-for="(candidate, index) in briefing.formation.candidates" :key="`${candidate.name}-${candidate.level}-${index}`">
            <strong>{{ candidate.name }}</strong>
            <span class="briefing-tags">
              <i>{{ candidate.type }}</i>
              <i>{{ candidate.level }} 级</i>
              <i>{{ formLabel(candidate.form) }}</i>
              <i :class="{ tired: candidate.fatigue != null && candidate.fatigue < 20 }">{{ fatigueLabel(candidate.fatigue) }}</i>
              <i v-if="candidate.injury" class="injured">{{ candidate.injury }}</i>
            </span>
          </li>
        </ol>
        <p v-else class="briefing-empty">刀帐里没挑出能上的刀——先去提升练度，或回「刀剑」页跑一次盘点。</p>
        <p v-if="briefing.formation.train_note" class="briefing-train">{{ briefing.formation.train_note }}</p>
      </section>

      <section v-if="briefing.troops.length" class="briefing-card" aria-label="刀装决策">
        <h4>刀装决策</h4>
        <table class="briefing-troops">
          <thead><tr><th>刀</th><th>刀种</th><th>刀装</th><th>为什么</th></tr></thead>
          <tbody>
            <tr v-for="advice in briefing.troops" :key="advice.name + advice.troop">
              <td>{{ advice.name }}</td>
              <td>{{ advice.type }}</td>
              <td><strong>{{ advice.troop }}</strong></td>
              <td>{{ advice.why }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section v-if="briefing.flower.length" class="briefing-card" aria-label="控花提示">
        <h4>控花提示</h4>
        <ul class="briefing-flower">
          <li v-for="tip in briefing.flower" :key="tip">{{ tip }}</li>
        </ul>
      </section>
    </div>
  </section>
</template>

<style scoped>
.briefing-panel { display: grid; gap: 10px; }
.briefing-form { display: grid; gap: 12px; padding: 16px 18px; background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 12px; }
.briefing-fields { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.briefing-form label { display: grid; gap: 5px; color: var(--ink-dim); font-size: 12px; }
.briefing-form input, .briefing-form select { width: 100%; min-width: 0; }
.briefing-spear { display: flex !important; align-items: center; gap: 8px !important; align-self: end; padding-bottom: 6px; }
.briefing-spear input { width: auto; }
.briefing-spear span { color: var(--ink); font-size: 13px; }
.briefing-bonus label { display: grid; gap: 5px; color: var(--ink-dim); font-size: 12px; }
.briefing-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.briefing-chip { padding: 3px 10px; color: var(--fox-gold-deep); background: color-mix(in srgb, #f4e8cf 75%, var(--paper-card)); border: 1px solid #d9bd84; border-radius: 999px; font-size: 12px; cursor: pointer; }
.briefing-bonus-empty { margin: 8px 0 0; color: var(--ink-dim); font-size: 12px; }
.briefing-actions { display: flex; gap: 8px; }
.briefing-error { margin: 0; padding: 10px 12px; color: #9f3d28; background: #f9e6df; border: 1px solid #d6a394; border-radius: 10px; }
.briefing-result { display: grid; gap: 10px; }
.briefing-summary { margin: 0; padding: 14px 16px; font-size: clamp(17px, 2.2vw, 22px); font-weight: 700; background: var(--paper-card); border: 1px solid var(--paper-line); border-left: 5px solid var(--fox-gold); border-radius: 12px; }
.briefing-warnings { padding: 12px 14px; color: #8f2f1c; background: color-mix(in srgb, #f4dfd7 78%, var(--paper-card)); border: 1px solid #cf8f7f; border-left: 5px solid #b0492e; border-radius: 12px; }
.briefing-warnings h4 { margin: 0 0 6px; font-size: 14px; }
.briefing-warnings ul { margin: 0; padding-left: 18px; display: grid; gap: 4px; font-size: 13px; font-weight: 700; }
.briefing-card { padding: 14px 16px; background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 12px; }
.briefing-card h4 { margin: 0 0 8px; font-size: 15px; }
.briefing-card h4 small { margin-left: 8px; color: var(--ink-dim); font-size: 12px; font-weight: 400; }
.briefing-wanted { margin: 0 0 8px; color: var(--fox-gold-deep); font-size: 12px; }
.briefing-candidates { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; counter-reset: candidate; }
.briefing-candidates li { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 7px 10px; border: 1px solid var(--paper-line); border-radius: 9px; }
.briefing-candidates li::before { counter-increment: candidate; content: counter(candidate); flex: none; width: 20px; height: 20px; display: grid; place-items: center; color: var(--ink-dim); background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 50%; font-size: 11px; }
.briefing-candidates strong { margin-right: auto; }
.briefing-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.briefing-tags i { padding: 1px 7px; color: var(--ink-dim); background: var(--paper-card); border: 1px solid var(--paper-line); border-radius: 999px; font-size: 10px; font-style: normal; white-space: nowrap; }
.briefing-tags i.tired { color: #9f3d28; background: color-mix(in srgb, #f4dfd7 70%, var(--paper-card)); border-color: #d8a195; }
.briefing-tags i.injured { color: #8f2f1c; background: color-mix(in srgb, #f4dfd7 78%, var(--paper-card)); border-color: #cf8f7f; font-weight: 700; }
.briefing-empty { margin: 0; color: var(--ink-dim); font-size: 12px; }
.briefing-train { margin: 10px 0 0; padding: 8px 10px; color: #426b36; background: color-mix(in srgb, #dcebd6 72%, var(--paper-card)); border: 1px solid #a8c59c; border-radius: 8px; font-size: 12px; }
.briefing-bonus-list { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; }
.briefing-verdict { padding: 8px 10px; border: 1px solid var(--paper-line); border-left-width: 4px; border-radius: 9px; }
.briefing-verdict header { display: flex; align-items: baseline; gap: 8px; }
.briefing-verdict em { padding: 1px 8px; border: 1px solid var(--paper-line); border-radius: 999px; font-size: 11px; font-style: normal; }
.briefing-verdict small { color: var(--ink-dim); font-size: 11px; }
.briefing-verdict p { margin: 4px 0 0; color: var(--ink-dim); font-size: 12px; }
.briefing-verdict.good { border-left-color: #5b813f; }
.briefing-verdict.good em { color: #426b36; background: color-mix(in srgb, #dcebd6 72%, var(--paper-card)); border-color: #a8c59c; }
.briefing-verdict.warn { border-left-color: var(--fox-gold); }
.briefing-verdict.warn em { color: var(--fox-gold-deep); background: color-mix(in srgb, #f4e8cf 75%, var(--paper-card)); border-color: #d9bd84; }
.briefing-verdict.bad { border-left-color: #b0492e; }
.briefing-verdict.bad em { color: #9f3d28; background: color-mix(in srgb, #f4dfd7 70%, var(--paper-card)); border-color: #d8a195; }
.briefing-verdict.dim { border-left-color: var(--paper-line); opacity: 0.92; }
.briefing-troops { width: 100%; border-collapse: collapse; font-size: 13px; }
.briefing-troops th, .briefing-troops td { padding: 6px 8px; text-align: left; border-bottom: 1px solid var(--paper-line); }
.briefing-troops th { color: var(--ink-dim); font-size: 11px; font-weight: 400; }
.briefing-troops tr:last-child td { border-bottom: 0; }
.briefing-flower { margin: 0; padding-left: 18px; display: grid; gap: 5px; color: var(--ink-dim); font-size: 13px; }
@media (max-width: 700px) {
  .briefing-fields { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .briefing-candidates li { align-items: flex-start; flex-direction: column; gap: 5px; }
  .briefing-candidates li::before { display: none; }
}
@media (max-width: 520px) {
  .briefing-form { padding: 14px; }
  .briefing-fields { grid-template-columns: 1fr; }
}
</style>
