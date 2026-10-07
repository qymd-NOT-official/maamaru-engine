<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps<{ server?: string; running: boolean; current: string | null; stopping: boolean; starting: boolean }>()
const emit = defineEmits<{ updated: []; error: [message: string]; runInventory: [] }>()
const syncing = ref(false)
const busy = computed(() => props.running || props.stopping || props.starting || syncing.value)
const inventoryLabel = computed(() => props.starting ? '正在启动……'
  : props.running && props.current === 'sword_inventory' ? '正在盘点……' : '截图盘点（备用）')

async function update() {
  syncing.value = true
  emit('error', '')
  try {
    if (props.server === 'jp') await api.jpListenerStart()
    else await api.refreshHonmaruSituation()
    emit('updated')
  } catch (cause) {
    emit('error', cause instanceof Error ? cause.message : '没能读到游戏名单')
  } finally {
    syncing.value = false
  }
}

watch(() => props.running, (running, previous) => {
  if (previous && !running) emit('updated')
})
</script>

<template>
  <div class="sword-read-actions">
    <button type="button" class="primary" :disabled="busy" :title="server === 'jp' ? '打开日服浏览器，进入结成后更新所持名单。' : '进入游戏本丸后，读取游戏所持名单。'" @click="update">{{ syncing ? '正在更新……' : '更新刀帐' }}</button>
    <details v-if="server !== 'jp'">
      <summary>更多</summary>
      <button type="button" class="secondary" :disabled="busy" title="操作游戏，逐页截图识别所持刀剑；读不全时保留原档案。" @click="emit('runInventory')">{{ inventoryLabel }}</button>
    </details>
  </div>
</template>

<style scoped>
.sword-read-actions { display: flex; align-items: flex-start; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
summary { cursor: pointer; color: var(--ink-dim); padding: 12px 4px; }
details button { margin-top: 6px; }
button { min-height: 38px; padding: 9px 15px; border: 1px solid var(--paper-line); border-radius: 7px; background: var(--paper-card); color: var(--ink); font: inherit; cursor: pointer; }
.primary { background: var(--fox-gold); border-color: var(--fox-gold-deep); color: #fffaf0; box-shadow: 0 3px 0 var(--fox-gold-deep); }
button:disabled { opacity: .55; cursor: wait; }
button:focus-visible, summary:focus-visible { outline: 2px solid var(--fox-gold); outline-offset: 3px; }
@media (max-width: 680px) { .sword-read-actions { justify-content: flex-start; } }
</style>
