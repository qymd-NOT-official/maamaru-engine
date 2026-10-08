<script setup lang="ts">
import { computed } from 'vue'
import ParamField from './ParamField.vue'
import PaperCard from './PaperCard.vue'
import type { ParamField as Field, ScriptInfo, ScriptParams } from '../types'
import { matchesRule } from '../visibility'

const props = defineProps<{
  scriptKey: string
  info: ScriptInfo
  modelValue: ScriptParams
  running: boolean
  busy: boolean
  hasAdvanced?: boolean
  advancedLabel?: string
}>()
const emit = defineEmits<{
  'update:modelValue': [value: ScriptParams]
  save: []
  run: []
  stop: []
}>()

const visibleFields = computed(() => props.info.params.filter(isVisible))

function isVisible(field: Field) {
  return matchesRule(field.visibleWhen, key => props.modelValue[key])
}

function update(key: string, value: unknown) {
  const next = { ...props.modelValue, [key]: value }

  /*
   * 一键日课的“出阵”按钮和“出阵安排”是同一个开关的两种入口：
   * 不出阵必须取消清单勾选；选了具体玩法必须勾上出阵，避免保存出互相矛盾的配置。
   */
  if (props.scriptKey === 'daily') {
    if (key === 'sortie_mode') {
      const steps = Array.isArray(next.steps) ? [...next.steps] : []
      const sortieIndex = steps.indexOf('出阵')
      if (String(value) === 'none' && sortieIndex >= 0) steps.splice(sortieIndex, 1)
      if (String(value) !== 'none' && sortieIndex < 0) {
        const rewardIndex = steps.indexOf('任务奖励')
        steps.splice(rewardIndex >= 0 ? rewardIndex : steps.length, 0, '出阵')
      }
      next.steps = steps
    } else if (key === 'steps' && Array.isArray(value)) {
      const hasSortie = value.includes('出阵')
      const currentMode = String(props.modelValue.sortie_mode ?? 'none')
      next.sortie_mode = hasSortie
        ? (currentMode === 'none' ? 'sortie' : currentMode)
        : 'none'
    }
  }

  emit('update:modelValue', next)
}
</script>

<template>
  <PaperCard variant="task">
    <header>
      <div>
        <h2>
          {{ info.label }}
          <button v-if="info.desc" type="button" class="help-trigger" :aria-label="`${info.label}说明`">
            ?
            <span class="help-tooltip" role="tooltip">{{ info.desc }}</span>
          </button>
        </h2>
      </div>
      <span class="status" :class="{ running }">{{ running ? '运行中' : '待命' }}</span>
    </header>
    <div class="fields">
      <ParamField
        v-for="field in visibleFields"
        :key="field.key"
        :field="field"
        :model-value="modelValue[field.key]"
        @update:model-value="update(field.key, $event)"
      />
    </div>
    <div v-if="!visibleFields.length && !hasAdvanced" class="task-empty-state">
      <strong>这个功能无需设置</strong>
      <span>可以直接在这里启动，也可以回执务台把它放进常用功能。</span>
    </div>
    <div v-if="hasAdvanced" class="advanced-settings-slot">
      <span class="advanced-settings-label">{{ advancedLabel || '特有高级设置' }}</span>
      <slot name="advanced" />
    </div>
    <footer>
      <button v-if="visibleFields.length || hasAdvanced" type="button" class="secondary" @click="emit('save')">保存配置</button>
      <button v-if="running" type="button" class="danger" @click="emit('stop')">紧急停止</button>
      <button v-else type="button" class="primary" :disabled="busy" @click="emit('run')">
        {{ busy ? '有其他任务正在运行' : '开始任务' }}
      </button>
    </footer>
  </PaperCard>
</template>
