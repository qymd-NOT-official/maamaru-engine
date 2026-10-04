<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
import PixelControl from './PixelControl.vue'
import type { CustomFormation } from '../types'

const config = ref<any>(null)
const formations = ref<CustomFormation[]>([])
const teams = [1, 2, 3, 4, 5]

function mapLabel(map: any) { return `${map.code} · ${map.name}（${map.duration_text}）` }
function completeRows() {
  const rows = new Map((config.value.common_plan || []).map((row: any) => [Number(row.team_no), row]))
  config.value.common_plan = teams.map(team => rows.get(team) || { team_no: team, map_code: config.value.maps[0]?.code || '', enabled: false })
}
function formationOptions(teamNo: number) {
  return formations.value.filter(item => item.target_team === teamNo)
}
async function load() {
  const [schedule, presets] = await Promise.all([
    api.expeditionSchedule(), api.customFormations(),
  ])
  config.value = schedule
  formations.value = presets.formations || []
  completeRows()
}
async function save() {
  if (!config.value) throw new Error('远征安排还在加载，请稍后再试')
  await api.saveExpeditionSchedule({ entries: config.value.entries, common_plan: config.value.common_plan, automation: config.value.automation })
}
defineExpose({ save })
onMounted(load)
</script>

<template>
  <section v-if="config" class="immediate-expedition">
    <div class="immediate-expedition-head"><div><h3>立刻远征</h3><p>运行“远征”后立即派出，不会到点自动接管游戏。队伍安排随本页配置一起保存。</p></div></div>
    <label class="expedition-sakura-toggle"><input v-model="config.automation.sakura_before_dispatch" type="checkbox" /> 远征续派前补花</label>
    <p class="field-help">优先刷这队上锁、等级大于1、疲劳≤49的刀，恢复队伍后再出发。伤势条件沿用刷花配置；一键日课也会照此执行。</p>
    <div v-for="row in config.common_plan" :key="row.team_no" class="setting-row expedition-row">
      <label><input v-model="row.enabled" type="checkbox" /> 部队{{ row.team_no }}</label>
      <PixelControl v-model="row.map_code" as="select"><option v-for="map in config.maps" :key="map.code" :value="map.code">{{ mapLabel(map) }}</option></PixelControl>
      <PixelControl v-model="row.formation_id" as="select" aria-label="出发前套用的部队预设">
        <option value="">保持当前成员</option>
        <option v-for="preset in formationOptions(row.team_no)" :key="preset.id" :value="preset.id">套用：{{ preset.name }}</option>
      </PixelControl>
    </div>
  </section>
</template>

<style scoped>
.expedition-sakura-toggle { display: flex; align-items: center; gap: .5rem; margin: 1rem 0 .5rem; }
.expedition-sakura-toggle input { width: auto; margin: 0; }
</style>
