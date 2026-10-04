<script setup lang="ts">
// 近侍舞台的演员层：狐之助 + 小狐丸（透明像素立绘）。
// 待命期间两位会随机串门打招呼；任务跑完时追加一次收工寒暄。
// 互动只改 CSS class，动画本体全部在 style.css，主题换皮不影响状态机。
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps<{ active: boolean }>()

type Phase = 'idle' | 'approach' | 'chat' | 'leave'
type ChatLine = { who: 'fox' | 'kogi'; text: string }

const phase = ref<Phase>('idle')
const foxLine = ref('')
const kogiLine = ref('')
const clickLines = {
  fox: ['嘿嘿，毛都被戳蓬了！', '小狐狸在呢！', '要一起喝茶吗？'],
  kogi: ['哦呀，叫我吗？', '毛发可要轻些摸。', '来杯茶，再配些油豆腐吧。'],
}
const bounces = new Map<HTMLElement, Animation>()

function poke(who: 'fox' | 'kogi', event: MouseEvent) {
  const actor = event.currentTarget as HTMLElement
  bounces.get(actor)?.cancel()
  if (!reducedMotion.matches) {
    const squash = who === 'fox' ? 0.77 : 0.84
    const bounce = actor.animate([
      { scale: '1 1', offset: 0 },
      { scale: `1.18 ${squash}`, offset: 0.18 },
      { scale: '0.88 1.16', offset: 0.43 },
      { scale: '1.06 0.94', offset: 0.66 },
      { scale: '0.98 1.03', offset: 0.84 },
      { scale: '1 1', offset: 1 },
    ], { duration: 560, easing: 'ease-in-out' })
    bounces.set(actor, bounce)
    bounce.onfinish = () => { if (bounces.get(actor) === bounce) bounces.delete(actor) }
  }
  clearTimers()
  foxLine.value = who === 'fox' ? pick(clickLines.fox) : ''
  kogiLine.value = who === 'kogi' ? pick(clickLines.kogi) : ''
  later(() => {
    phase.value = 'idle'
    foxLine.value = ''
    kogiLine.value = ''
    scheduleNext()
  }, 4000)
}

const idleChats: ChatLine[][] = [
  [
    { who: 'fox', text: '小狐丸大人——！' },
    { who: 'kogi', text: '哦呀，是狐狸吗。' },
    { who: 'fox', text: '今天的本丸也很和平呢！' },
  ],
  [
    { who: 'kogi', text: '狐狸，毛色不错。' },
    { who: 'fox', text: '嘿嘿，被夸了！' },
  ],
  [
    { who: 'fox', text: '要一起喝茶吗？' },
    { who: 'kogi', text: '好啊，配油豆腐就更好了。' },
  ],
]
const finishChats: ChatLine[][] = [
  [
    { who: 'fox', text: '任务完成啦！' },
    { who: 'kogi', text: '辛苦了，来杯茶吧。' },
  ],
  [
    { who: 'kogi', text: '做得漂亮。' },
    { who: 'fox', text: '都是主人的功劳！' },
  ],
]

const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)')
let timers: number[] = []
let disposed = false

function later(fn: () => void, ms: number) {
  timers.push(window.setTimeout(() => { if (!disposed) fn() }, ms))
}
function clearTimers() {
  timers.forEach(id => window.clearTimeout(id))
  timers = []
}
function pick<T>(list: T[]): T {
  return list[Math.floor(Math.random() * list.length)]
}

function scheduleNext() {
  if (props.active || reducedMotion.matches) return
  later(() => startInteraction(pick(idleChats)), 45000 + Math.random() * 45000)
}

function startInteraction(lines: ChatLine[]) {
  if (props.active || reducedMotion.matches || phase.value !== 'idle') return
  phase.value = 'approach'
  later(() => {
    phase.value = 'chat'
    let t = 0
    for (const line of lines) {
      later(() => {
        if (line.who === 'fox') foxLine.value = line.text
        else kogiLine.value = line.text
      }, t)
      t += 2300
    }
    later(() => {
      phase.value = 'leave'
      foxLine.value = ''
      kogiLine.value = ''
    }, t + 400)
    later(() => {
      phase.value = 'idle'
      scheduleNext()
    }, t + 1800)
  }, 1500)
}

function cancelInteraction() {
  clearTimers()
  phase.value = 'idle'
  foxLine.value = ''
  kogiLine.value = ''
}

watch(() => props.active, (now, before) => {
  if (now) {
    cancelInteraction()
  } else if (before) {
    // 刚收工：尽快安排一次庆祝寒暄
    later(() => startInteraction(pick(finishChats)), 2500)
  } else {
    scheduleNext()
  }
})

onMounted(() => { if (!props.active) scheduleNext() })
onBeforeUnmount(() => { disposed = true; clearTimers(); bounces.forEach(animation => animation.cancel()) })
</script>

<template>
  <div class="stage-actors" :class="`phase-${phase}`">
    <button type="button" class="stage-kogi" aria-label="和小狐丸打招呼" @click="poke('kogi', $event)"></button>
    <button type="button" class="stage-fox" aria-label="和小狐狸打招呼" @click="poke('fox', $event)"></button>
    <div v-if="kogiLine" class="stage-bubble bubble-kogi" role="status">{{ kogiLine }}</div>
    <div v-if="foxLine" class="stage-bubble bubble-fox" role="status">{{ foxLine }}</div>
  </div>
</template>
