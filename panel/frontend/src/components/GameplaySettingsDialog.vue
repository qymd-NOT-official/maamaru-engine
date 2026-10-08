<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { api } from '../api'
import { matchesRule } from '../visibility'
import ParamField from './ParamField.vue'
import type { ScriptInfo, ScriptParams } from '../types'

const emit = defineEmits<{ saved: [script: string, params: ScriptParams] }>()
const dialog = ref<HTMLDialogElement>()
const script = ref('')
const info = ref<ScriptInfo>()
const params = ref<ScriptParams>({})
const busy = ref(false)
const message = ref('')
const schedule = ref<{time: string; runs: number; choices?: {script: string; label: string; available: boolean}[]; choose?: (script: string, time: string, runs: number) => void; save: (params: ScriptParams, time: string, runs: number) => Promise<ScriptParams>}>()
let requestId = 0
const fields = computed(() => (info.value?.params || []).filter(field =>
  !['runs', 'rounds', 'loops', 'floors', 'max_runs', 'refill_run_limit'].includes(field.key)
  && !(schedule.value && script.value === 'raid' && field.key === 'auto_refill')
  && matchesRule(field.visibleWhen, key => params.value[key])))

async function open(key: string, booking?: typeof schedule.value) {
  schedule.value = booking ? { ...booking } : undefined
  const id = ++requestId
  script.value = key
  info.value = undefined
  params.value = {}
  message.value = ''
  busy.value = true
  await nextTick()
  if (!dialog.value?.open) dialog.value?.showModal()
  try {
    const result = await api.gameplaySettings(key)
    if (id !== requestId) return
    info.value = result.info
    params.value = result.params
  } catch (error) {
    message.value = error instanceof Error ? error.message : '设置没读到，请关闭后重试'
  } finally {
    if (id === requestId) busy.value = false
  }
}

function close() {
  if (busy.value) return
  ++requestId
  dialog.value?.close()
}

async function save() {
  if (!info.value || busy.value) return
  busy.value = true
  message.value = ''
  try {
    if (schedule.value && (!schedule.value.time || !Number.isInteger(schedule.value.runs) || schedule.value.runs < 1 || schedule.value.runs > 99)) throw new Error('请填写开始时间和1–99次出阵')
    const savedParams = schedule.value
      ? await schedule.value.save(params.value, schedule.value.time, schedule.value.runs)
      : (await api.saveGameplaySettings(script.value, params.value)).params
    emit('saved', script.value, savedParams)
    dialog.value?.close()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '没存上，请重试'
  } finally {
    busy.value = false
  }
}
defineExpose({ open })
</script>

<template>
  <dialog ref="dialog" class="gameplay-dialog" aria-labelledby="gameplay-dialog-title"
    @cancel.prevent="close" @click="($event.target === dialog) && close()">
    <header>
      <h2 id="gameplay-dialog-title">{{ info?.label || '读取中…' }} · {{ schedule ? '配置' : '玩法设置' }}</h2>
      <button type="button" :disabled="busy" aria-label="关闭玩法设置" @click="close">关闭</button>
    </header>
    <label v-if="schedule?.choices" class="gameplay-choice">玩法
      <select :value="script" :disabled="busy" @change="schedule.choose?.(($event.target as HTMLSelectElement).value, schedule.time, schedule.runs)">
        <option v-for="option in schedule.choices" :key="option.script" :value="option.script" :disabled="!option.available">{{ option.label }}{{ option.available ? '' : '（未开放）' }}</option>
      </select>
    </label>
    <div v-if="schedule" class="gameplay-fields schedule-fields">
      <label>开始时间<input v-model="schedule.time" type="time" :disabled="busy" /></label>
      <label>圈数<input v-model.number="schedule.runs" type="number" min="1" max="99" :disabled="busy" /></label>
    </div>
    <p v-if="schedule" class="note">00:00–03:59 为次日凌晨；保存后按这段时间和圈数安排出阵。</p>
    <p v-if="schedule && script === 'raid'" class="note">今日联队战自动使用小判补充手形，跑够本段圈数；不会改动单跑的补充开关。</p>
    <p v-else class="note">与玩法页共用设置；本段出阵次数以时间表为准。保存后还需保存时间表，才会到点开工。</p>
    <div class="gameplay-fields">
      <ParamField v-for="field in fields" :key="field.key" :field="field"
        :model-value="params[field.key]" @update:model-value="params[field.key] = $event" />
    </div>
    <p v-if="message" role="alert">{{ message }}</p>
    <footer>
      <button type="button" :disabled="busy || !info" @click="save">{{ busy ? '请稍候…' : schedule ? '保存' : '保存并关闭' }}</button>
      <button type="button" :disabled="busy" @click="close">取消</button>
    </footer>
  </dialog>
</template>

<style scoped>
.gameplay-dialog { width: min(680px, calc(100vw - 32px)); max-height: calc(100dvh - 40px); box-sizing: border-box; overflow: auto; padding: 24px; border: 1px solid var(--paper-line); border-radius: 14px; color: var(--ink); background: var(--paper-card); }
.gameplay-dialog::backdrop { background: rgb(0 0 0 / 38%); }
header, footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h2 { margin: 0; font-size: 18px; }
.schedule-fields label { display: grid; gap: 8px; }
.schedule-fields input { min-width: 0; padding: 9px 12px; border: 1px solid var(--paper-line); border-radius: 8px; background: var(--paper-panel); color: var(--ink); font: inherit; }
.note { font-size: 13px; line-height: 1.7; }
.gameplay-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin: 20px 0; }
footer { justify-content: flex-start; }
header button, footer button { padding: 9px 14px; border: 1px solid var(--paper-line); border-radius: 8px; color: var(--ink); background: var(--paper-card); font: inherit; cursor: pointer; }
footer button:first-child { border-color: var(--fox-gold); background: var(--paper-panel); }
header button:disabled, footer button:disabled { cursor: wait; opacity: .6; }
header button:focus-visible, footer button:focus-visible { outline: 2px solid var(--fox-gold); outline-offset: 3px; }
@media (max-width: 560px) { .gameplay-dialog { padding: 18px; } .gameplay-fields { grid-template-columns: 1fr; } }
</style>
