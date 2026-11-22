<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api'
import PanelHeader from './PanelHeader.vue'
import UiIcon from './UiIcon.vue'

const props = withDefaults(defineProps<{ running?: boolean; stopping?: boolean; taskLabel?: string; onlyScript?: string }>(), { running: false, stopping: false, taskLabel: '', onlyScript: '' })
const runStatus = computed(() => props.stopping ? '正在停止…' : props.running ? `${props.taskLabel || '任务'}正在执行` : '空闲')

const entries = ref<any[]>([])
const raw = ref(false)
const autoScroll = ref(true)
const feedbackFailures = ref(0)
const feedbackDisabled = ref(false)
const showIssueButton = ref(false)
const list = ref<HTMLElement | null>(null)
let source: EventSource | null = null
let feedbackResetTimer = 0

const visibleEntries = computed(() => entries.value.filter(entry =>
  (!props.onlyScript || entry.script === props.onlyScript)
  && (raw.value || !/^\[(?:NAV|ADB|MAA)\]/.test(entry.message))))
const feedbackLabel = computed(() => feedbackDisabled.value ? '狐之助已下班' : '反馈错误')
const issueUrl = 'https://github.com/qymd-NOT-official/maamaru-engine/issues/new'
const feedbackLines: Record<number, string> = {
  1: '导出失败？问问上天',
  2: '还失败？去issue骂作者',
  4: '干嘛不去？',
  5: '你是不是想骂连错误处理系统都做不好？',
  6: '噫吁嚱，惶恐滩头说惶恐，零丁洋里叹零丁。',
  7: '面包店里卖面包，蛋糕店里卖蛋糕。',
  8: '你还点',
  9: '？',
  10: '我没有日志，你也不去issue，你到底想让我怎样',
}

const scriptNames: Record<string, string> = {
  daily: '一键日课', pumpkin: '南瓜', raid: '联队战', sortie: '合战场',
  yosari: '异去', osaka: '挖地', expedition: '远征', practice: '演练', smith: '锻刀',
  sakura: '刷花', sugar: '炼糖', repair: '手入', snapshot: '库存',
  rotate_captain: '换队长', formation: '编队换人', apply_formation_preset: '套预设',
  scheduler: '排班', system: '系统',
}

function clean(message: string) {
  return raw.value ? message : String(message).replace(/^\[[^\]]+\]\s*/, '')
}
function scriptName(script: string) { return raw.value ? script : (scriptNames[script] || script) }
function time(ts: number) { return ts ? new Date(ts * 1000).toLocaleTimeString() : '' }
function level(message: string) {
  if (/🛑|✗|翻车|异常退出/.test(message)) return 'bad'
  if (/⚠/.test(message)) return 'warn'
  if (/✓|✅|完成|成功|已领/.test(message)) return 'ok'
  return ''
}
async function exportFeedback() {
  if (feedbackDisabled.value) return
  try {
    // 面板套在 pywebview 里，浏览器下载会被 WebView 静默吞掉（点两下毫无反应
    // 的翻车现场）；让后端把反馈包落盘，并在资源管理器里替用户选好。
    const response = await fetch('/api/diagnostics/export-local', { method: 'POST' })
    if (response.status === 404) {
      // 旧版后端没有这个接口，退回浏览器原生下载
      const probe = await fetch('/api/diagnostics/export')
      if (!probe.ok) throw new Error(`feedback export failed (${probe.status})`)
      window.location.assign('/api/diagnostics/export')
      feedbackFailures.value = 0
      showIssueButton.value = false
      return
    }
    const data = await response.json().catch(() => null)
    if (!response.ok || !data?.ok) throw new Error(data?.message || `feedback export failed (${response.status})`)
    feedbackFailures.value = 0
    showIssueButton.value = true
    window.alert(`错误反馈包已生成，并在文件夹里替你选好了：\n${data.filename}\n\n去 Issue 发帖时把它附上就行。`)
  } catch (_) {
    feedbackFailures.value += 1
    if (feedbackFailures.value === 3) {
      showIssueButton.value = true
      return
    }
    if (feedbackFailures.value >= 11) {
      feedbackDisabled.value = true
      window.clearTimeout(feedbackResetTimer)
      feedbackResetTimer = window.setTimeout(() => {
        feedbackDisabled.value = false
        feedbackFailures.value = 0
        showIssueButton.value = false
      }, 3000)
      return
    }
    window.alert(feedbackLines[feedbackFailures.value] || '导出失败')
  }
}
function openIssue() { window.open(issueUrl, '_blank', 'noopener,noreferrer') }
async function scrollEnd() { await nextTick(); if (autoScroll.value) list.value?.scrollTo({ top: list.value.scrollHeight }) }
async function load() { entries.value = (await api.logs()).logs || []; scrollEnd() }
function connect() {
  source = new EventSource('/api/logs/stream')
  source.onmessage = event => {
    if (!event.data) return
    try {
      const entry = JSON.parse(event.data)
      entries.value.push(entry)
      if (entries.value.length > 500) entries.value.shift()
      if (entry.script === 'scheduler' && String(entry.message || '').startsWith('⏳')) {
        window.dispatchEvent(new CustomEvent('maamaru:scheduler-warning', { detail: entry.message }))
      }
      scrollEnd()
    } catch (_) {}
  }
  source.onerror = () => { source?.close(); source = null; window.setTimeout(connect, 3000) }
}
onMounted(() => { load(); connect() })
onBeforeUnmount(() => { source?.close(); window.clearTimeout(feedbackResetTimer) })
</script>

<template>
  <section class="log-panel" :class="raw ? 'raw-mode' : 'visual-mode'">
    <PanelHeader title="日志" :subtitle="`${visibleEntries.length} 条`">
      <template #actions>
      <div class="head-actions">
        <button class="secondary" :class="{ active: !raw }" :aria-pressed="!raw" @click="raw = false"><UiIcon name="cards" />可视化</button>
        <button class="secondary" :class="{ active: raw }" :aria-pressed="raw" @click="raw = true"><UiIcon name="code" />源码日志</button>
        <button class="secondary" @click="entries = []"><UiIcon name="eraser" />清屏</button>
        <button class="secondary" :class="{ active: autoScroll }" :aria-pressed="autoScroll" @click="autoScroll = !autoScroll"><UiIcon name="follow" />自动滚动</button>
        <button class="secondary" :disabled="feedbackDisabled" title="整理错误信息并下载可附在 Issue 中的 ZIP" @click="exportFeedback"><UiIcon name="download" />{{ feedbackLabel }}</button>
        <button v-if="showIssueButton" class="secondary issue-button" @click="openIssue"><UiIcon name="external" />去 Issue</button>
      </div>
      </template>
    </PanelHeader>
    <div ref="list" class="log-list">
      <div v-for="entry in visibleEntries" :key="entry.id" class="log-row" :class="level(entry.message)">
        <time>{{ time(entry.ts) }}</time><b>{{ scriptName(entry.script) }}</b><span>{{ clean(entry.message) }}</span>
      </div>
    </div>
    <footer class="log-status-bar"><span>{{ visibleEntries.length }} 条进度</span><span role="status" :title="runStatus">● {{ runStatus }}</span></footer>
  </section>
</template>
