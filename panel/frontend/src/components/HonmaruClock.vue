<script setup lang="ts">
import { computed } from 'vue'
import type { DayTimeline, HonmaruSituation } from '../types'
import { clockSegments, situationSegments } from './homeClockModel'
const props = defineProps<{ timeline: DayTimeline | null; now: number; situation?: HonmaruSituation | null; readOnly?: boolean }>()
const emit = defineEmits<{ open: [] }>()
const segments = computed(() => props.readOnly ? situationSegments(props.situation || null, props.now) : clockSegments(props.timeline, props.now))
const minute = computed(() => {
  const d = new Date(props.now)
  return d.getUTCHours() * 60 + d.getUTCMinutes() + d.getUTCSeconds() / 60 + 480
})
const clockTime = computed(() => new Date(props.now).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Shanghai' }))
function point(minute: number, radius: number) {
  const angle = minute / 1440 * Math.PI * 2 - Math.PI / 2
  return { x: 110 + radius * Math.cos(angle), y: 110 + radius * Math.sin(angle) }
}
function arc(duration: number, radius: number) {
  const total = 2 * Math.PI * radius
  return `${total * Math.min(duration, 1440) / 1440} ${total}`
}
</script>

<template>
  <button class="home-clock" type="button" :aria-label="readOnly ? '本丸时间表：已同步倒计时' : '查看今天的时间表'" :aria-disabled="readOnly || undefined" @click="!readOnly && emit('open')">
    <span class="clock-heading">今天的时间表 <time>{{ clockTime }}</time></span>
    <svg viewBox="0 0 220 220" role="img" :aria-label="readOnly ? `本丸时间表，现在 ${clockTime}，显示已同步倒计时` : `24小时时间表，现在 ${clockTime}，外圈远征，内圈任务，04:00日课刷新`">
      <circle cx="110" cy="110" r="103" class="dial" />
      <circle cx="110" cy="110" r="77" class="inner-dial" />
      <line v-for="hour in 24" :key="`tick-${hour}`" :x1="point(hour * 60, hour % 6 === 0 ? 94 : 99).x" :y1="point(hour * 60, hour % 6 === 0 ? 94 : 99).y" :x2="point(hour * 60, 103).x" :y2="point(hour * 60, 103).y" class="tick" />
      <text v-for="hour in 12" :key="`hour-${hour}`" :x="point((hour - 1) * 120, 85).x" :y="point((hour - 1) * 120, 85).y" class="hour">{{ String((hour - 1) * 2).padStart(2, '0') }}</text>
      <template v-for="segment in segments" :key="segment.key">
        <circle v-if="segment.duration > 0" cx="110" cy="110" :r="segment.lane === 'outer' ? 97 : 67" fill="none" stroke-width="6" stroke-linecap="round" :stroke-dasharray="arc(segment.duration, segment.lane === 'outer' ? 97 : 67)" :transform="`rotate(${segment.start / 4 - 90} 110 110)`" :class="['segment', segment.tone]">
          <title>{{ segment.label }} · {{ Math.round(segment.duration) }} 分钟</title>
        </circle>
        <circle v-else :cx="point(segment.start, 67).x" :cy="point(segment.start, 67).y" r="3" :class="['segment', segment.tone]"><title>{{ segment.label }}</title></circle>
      </template>
      <line v-if="!readOnly" :x1="point(240, 92).x" :y1="point(240, 92).y" :x2="point(240, 103).x" :y2="point(240, 103).y" class="refresh-mark"><title>04:00 日课刷新</title></line>
      <line x1="110" y1="110" :x2="point(minute, 89).x" :y2="point(minute, 89).y" class="hand" />
      <circle cx="110" cy="110" r="3" class="hand-center" />
    </svg>
    <span class="clock-key">{{ readOnly ? '已同步倒计时 · 不含自动排班' : '外圈远征 · 内圈任务' }}</span>
  </button>
</template>

<style scoped>
.home-clock { display: block; width: 100%; padding: 0; border: 0; background: transparent; color: var(--ink); text-align: left; cursor: pointer; }
.home-clock[aria-disabled="true"] { cursor: default; }
.clock-heading { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; font-weight: 600; }
.clock-heading time { color: #a13e32; font-variant-numeric: tabular-nums; font-weight: 400; }
svg { display: block; width: 100%; max-width: 300px; margin: 12px auto 8px; }
.dial { fill: #f2ead5; stroke: #d3c7ac; stroke-width: 1.2; }
.inner-dial { fill: none; stroke: #dfd2b4; stroke-width: .6; }
.tick { stroke: #b4a78c; stroke-width: .7; }
.hour { fill: #a3977e; font-size: 7px; text-anchor: middle; dominant-baseline: middle; }
.segment.expedition { stroke: #b8bca7; }
.segment.inactive { stroke: #cec5b0; opacity: .55; }
.segment.task { stroke: #aa781c; }
.segment.running { stroke: #315d43; }
.segment.planned { stroke: #a4b79a; fill: #a4b79a; }
circle.segment[stroke-dasharray] { fill: none; }
.segment.failed { stroke: #a34435; }
.hand, .hand-center { stroke: #a13e32; fill: #a13e32; stroke-width: 1.3; }
.refresh-mark { stroke: #aa781c; stroke-width: 1.8; }
.clock-key { display: block; color: var(--ink-dim); font-size: 10px; text-align: center; }
</style>
