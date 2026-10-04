<script setup lang="ts">
import { computed } from 'vue'
import type { ParamField, ScriptInfo, ScriptParams } from '../types'
import { matchesRule } from '../visibility'

const props = defineProps<{
  info: ScriptInfo
  iconSrc?: string
  params: ScriptParams
  running: boolean
  busy: boolean
}>()
const emit = defineEmits<{ run: []; stop: []; configure: [] }>()

function visible(field: ParamField) {
  if (field.type === 'note') return false
  return matchesRule(field.visibleWhen, key => props.params[key])
}

function display(field: ParamField) {
  const value = props.params[field.key]
  if (Array.isArray(value)) return `${value.length} 项`
  if (field.type === 'select') {
    const option = field.options?.find(item => String(Array.isArray(item) ? item[0] : item) === String(value))
    if (option) return Array.isArray(option) ? option[1] : option
  }
  return String(value ?? '未设置')
}

const summary = computed(() => props.info.params.filter(visible).slice(0, 4))
</script>

<template>
  <article class="overview-task-card">
    <header>
      <div><h2><img v-if="iconSrc" :src="iconSrc" alt="">{{ info.label }}</h2><p>{{ info.desc }}</p></div>
      <span class="status" :class="{ running }">{{ running ? '运行中' : '待命' }}</span>
    </header>
    <div class="overview-summary">
      <span v-for="field in summary" :key="field.key"><small>{{ field.label }}</small><b>{{ display(field) }}</b></span>
    </div>
    <button class="overview-details" type="button" @click="emit('configure')">▸ 查看全部设置</button>
    <footer>
      <button class="secondary" @click="emit('configure')">⚙ 调整配置</button>
      <button v-if="running" class="danger" @click="emit('stop')">紧急停止</button>
      <button v-else class="primary" :disabled="busy" @click="emit('run')">{{ busy ? '有任务运行中' : '开始任务' }}</button>
    </footer>
  </article>
</template>
