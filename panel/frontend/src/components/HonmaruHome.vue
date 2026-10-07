<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import { buildJournalPosts } from '../honmaruJournal'
import type { JournalPost } from '../honmaruJournal'
import type { DayTimeline, EventTimelineEntry, EventTimelineReport, HonmaruNote, HonmaruProfile, HonmaruSituation, PlanningReport } from '../types'
import PaperCard from './PaperCard.vue'
import HonmaruClock from './HonmaruClock.vue'
import HonmaruStatus from './HonmaruStatus.vue'
import { homeMoments, remainingTime } from './homeClockModel'
import { activityTitle, activityStep, eventTime, runTitle, runStatusLabel, shanghaiDate, signed } from './report/reportModel'

const props = defineProps<{ activity: any; busy: boolean; server?: string }>()
const isJp = computed(() => props.server === 'jp')
const emit = defineEmits<{ office: []; report: []; records: []; planning: []; resumeRaid: [runId: string] }>()
const emptyProfile = (): HonmaruProfile => ({ honmaru_name: '', saniwa_name: '', province: '', attendant: '', motto: '', joined_on: '', avatar: '' })
const profile = ref<HonmaruProfile>(emptyProfile())
const draft = ref<HonmaruProfile>(emptyProfile())
const notes = ref<HonmaruNote[]>([])
const runs = ref<any[]>([])
const planning = ref<PlanningReport | null>(null)
const timeline = ref<EventTimelineReport | null>(null)
const inventory = ref<any>(null)
const dayPlan = ref<DayTimeline | null>(null)
const interruptedRaids = computed(() => dayPlan.value?.conductor.blocks.filter(block => block.recovery && block.run_id) || [])
let dayPlanTimer: number | undefined
async function refreshDayPlan() {
  try {
    if (isJp.value) {
      loadErrors.value = await loadSummaries()
      return
    }
    dayPlan.value = await api.dayTimeline()
  } catch { /* keep the last known arrangement */ }
}
const journalEvents = ref<any[]>([])
const swordDepartures = ref<any[]>([])
const situation = ref<HonmaruSituation | null>(null)
const syncingSituation = ref(false)
const launchingGame = ref(false)
const situationError = ref('')
const editingProfile = ref(false)
const writing = ref(false)
const noteBody = ref('')
const noteId = ref<string | undefined>()
const savingProfile = ref(false)
const savingNote = ref(false)
const homeReady = ref(false)
const loading = ref(true)
const loadErrors = ref<string[]>([])
const formError = ref('')
const notice = ref('')
const filter = ref<'all' | 'notes'>('all')
const limit = ref(8)
const now = ref(Date.now())
let timer = 0
const editor = ref<HTMLDialogElement | null>(null)
watch(() => editingProfile.value || writing.value, async open => {
  if (!open) return
  await nextTick()
  editor.value?.showModal()
  editor.value?.querySelector<HTMLInputElement | HTMLTextAreaElement>('input:not([type="file"]), textarea')?.focus()
})
const today = computed(() => shanghaiDate(now.value / 1000))
const todayLabel = computed(() => new Date(`${today.value}T12:00:00+08:00`).toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long', timeZone: 'Asia/Shanghai' }))
const daysTogether = computed(() => {
  if (!profile.value.joined_on) return null
  const days = Math.floor((Date.parse(`${today.value}T00:00:00+08:00`) - Date.parse(`${profile.value.joined_on}T00:00:00+08:00`)) / 86400000) + 1
  return Number.isFinite(days) && days > 0 ? days.toLocaleString() : null
})
const welcome = computed(() => profile.value.saniwa_name ? `${profile.value.saniwa_name}，欢迎回来。` : '欢迎回到本丸。')
const active = computed(() => props.busy || props.activity?.active)
const currentActivityTitle = computed(() => activityTitle(props.activity))
const currentActivityStep = computed(() => activityStep(props.activity))
const homeName = computed(() => profile.value.honmaru_name || (isJp.value ? '日服本丸' : '我的本丸'))
const entries = computed(() => {
  const personal = notes.value.map(note => ({ key: `note-${note.id}`, ts: note.created_at, note, run: null as any, post: null as JournalPost | null }))
  const work = filter.value === 'notes' ? [] : runs.value.map(run => ({ key: `run-${run.run_id}`, ts: Number(run.ended_at || run.started_at), note: null as HonmaruNote | null, run, post: null as JournalPost | null }))
  const posts = filter.value === 'notes' ? [] : buildJournalPosts(journalEvents.value, swordDepartures.value)
    .map(post => ({ key: post.key, ts: post.ts, note: null as HonmaruNote | null, run: null as any, post }))
  return [...personal, ...work, ...posts].sort((a, b) => b.ts - a.ts)
})
const visibleEntries = computed(() => entries.value.slice(0, limit.value))
const groups = computed(() => {
  const result: Array<{ date: string; entries: typeof visibleEntries.value }> = []
  for (const entry of visibleEntries.value) {
    const date = shanghaiDate(entry.ts)
    const last = result[result.length - 1]
    if (last?.date === date) last.entries.push(entry)
    else result.push({ date, entries: [entry] })
  }
  return result
})
const resourceWatch = computed(() => planning.value?.resource_watch)
const kobanWatch = computed(() => planning.value?.koban_watch)
const recentKoban = computed<number | null>(() => {
  const value = kobanWatch.value?.current ?? inventory.value?.resources?.['小判']
  return typeof value === 'number' && Number.isFinite(value) ? value : null
})
const nearestEvent = computed<EventTimelineEntry | null>(() => timeline.value?.ongoing[0] || timeline.value?.upcoming[0] || null)
const activityPlan = computed(() => {
  const activity = dayPlan.value?.activity
  return activity && activity.name === nearestEvent.value?.name ? activity : null
})
const situationMoments = computed(() => homeMoments(situation.value, now.value))
const resourceNames = ['小判', '木炭', '玉钢', '冷却材', '砥石', '委托符', '加速符']
function fmt(value: number | null | undefined) { return value == null ? '尚未记录' : Math.round(value).toLocaleString() }
function resource(name: string) {
  const value = planning.value?.current?.[name] ?? inventory.value?.resources?.[name]
  return typeof value === 'number' ? value.toLocaleString() : '未记录'
}
function dateLabel(date: string) { return date === today.value ? '今天' : date.replaceAll('-', '.') }
function gameTime(value: string) { return value.slice(0, 16).replaceAll('-', '.') }
function situationTime(value: string | null) {
  if (!value) return '读取时间不明'
  const stamp = Date.parse(value.replace(' ', 'T') + '+08:00')
  if (!Number.isFinite(stamp)) return '读取时间不明'
  const minutes = Math.max(0, Math.floor((now.value - stamp) / 60000))
  return minutes < 60 ? `${minutes} 分钟前读取` : `${value.slice(5, 16).replace('-', '.')} 读取`
}
async function syncSituation() {
  syncingSituation.value = true
  situationError.value = ''
  try {
    if (isJp.value) {
      await api.jpListenerStart()
      notice.value = '日服浏览器已打开，进入本丸后会自动更新近况。'
    }
    situation.value = (await api.refreshHonmaruSituation(props.server)).situation
    loadErrors.value = await loadSummaries()
  }
  catch (error) { situationError.value = errorMessage(error) }
  finally { syncingSituation.value = false }
}
async function launchGame() {
  if (!isJp.value || launchingGame.value) return
  launchingGame.value = true
  notice.value = ''
  try {
    await api.jpListenerStart()
    notice.value = '日服浏览器已打开，进入游戏后会自动更新本丸数据。'
  } catch (error) { notice.value = errorMessage(error) }
  finally { launchingGame.value = false }
}
const runPostKinds: Record<string, { label: string; icon: string; scene: string }> = {
  sortie: { label: '出阵手记', icon: 'sortie.png', scene: 'honmaru_sortie_stage.png' },
  yosari: { label: '异去手记', icon: 'yosari.png', scene: 'honmaru_sortie_stage.png' },
  osaka: { label: '大阪城手记', icon: 'digging.png', scene: 'honmaru_sortie_stage.png' },
  edocastle: { label: '江户城手记', icon: 'edocastle.png', scene: 'honmaru_sortie_stage.png' },
  hanafuda: { label: '秘宝之里手记', icon: 'hanafuda.png', scene: 'honmaru_sortie_stage.png' },
  raid: { label: '联队战手记', icon: 'raid.png', scene: 'honmaru_sortie_stage.png' },
  pumpkin: { label: '南瓜手记', icon: 'pumpkin.png', scene: 'honmaru_sortie_stage.png' },
  expedition: { label: '远征来信', icon: 'expedition.png', scene: 'honmaru_garden_stage.png' },
  smith: { label: '锻刀手记', icon: 'forge.png', scene: 'honmaru_forge_stage.png' },
  daily: { label: '日课手记', icon: 'daily.png', scene: 'honmaru_garden_stage.png' },
}
function runPostKind(run: any) {
  return runPostKinds[String(run.script)] || { label: '本丸执务', icon: 'office.svg', scene: 'honmaru_garden_stage.png' }
}
function runPostText(run: any) {
  const loops = Number(run.loops) || 0
  if (run.status === 'failed') return `这趟没能顺利收工。${loops > 0 ? `已确认的 ${loops} 圈照常记下；` : ''}停在哪里，留在详细记录里了。`
  if (run.status === 'stopped') return `这趟按你的意思停下了。${loops > 0 ? `已确认走完 ${loops} 圈，` : ''}后面的安排不会算作完成。`
  if (run.status !== 'completed') return '这趟的结果还没确认，先照原样留在记录里。'
  if (loops > 0) return `这趟确认走完 ${loops} 圈。走过的路与能核对的收获，都留在账房的记录里。`
  return '这趟执务已经收工，完成了哪些事，可以翻开账房记录看看。'
}
function runPostFacts(run: any) {
  const facts: string[] = []
  const teams = [...new Set((Array.isArray(run.loop_records) ? run.loop_records : [])
    .map((record: any) => Number(record.team_no))
    .filter((team: number) => Number.isInteger(team) && team >= 1 && team <= 5))]
  if (teams.length === 1) facts.push(`第 ${teams[0]} 部队`)
  const changes = Object.entries(run.attributed_resource_delta || {})
    .filter(([, value]) => typeof value === 'number' && Number.isFinite(value) && value !== 0)
    .slice(0, 2)
  for (const [name, value] of changes) facts.push(`${name} ${signed(value as number)}`)
  return facts
}
function eventMoment(event: EventTimelineEntry) {
  if (timeline.value?.ongoing.includes(event)) {
    if (event.days_left === 0) return '今天结束'
    return event.days_left == null ? '进行中' : `还剩 ${event.days_left} 天`
  }
  if (event.days_until_start === 0) return '今天开始'
  if (event.days_until_start === 1) return '明天开始'
  return event.days_until_start == null ? '即将开始' : `${event.days_until_start} 天后开始`
}
function eventMomentLabel(event: EventTimelineEntry) { return timeline.value?.ongoing.includes(event) ? '结束' : '开始' }
function eventBudget(event: EventTimelineEntry) {
  if (!event.budget || event.budget.koban_cost == null) return '暂未核算'
  if (event.budget.koban_cost === 0) return '无需额外小判'
  if (event.budget.sufficient === true) return '已经备齐'
  if (event.budget.shortfall != null) return `还差 ${fmt(event.budget.shortfall)}`
  return '正在核算'
}
function errorMessage(error: unknown) { return error instanceof Error ? error.message : '暂时没能保存，请再试一次。' }

const executingToday = ref(false)
const todayExecutionMessage = ref('')
async function executeToday() {
  if (isJp.value || executingToday.value) return
  executingToday.value = true
  todayExecutionMessage.value = ''
  try {
    todayExecutionMessage.value = (await api.executeToday()).message
    await loadSummaries()
  } catch (error) {
    todayExecutionMessage.value = errorMessage(error)
    if (todayExecutionMessage.value.includes('中断')) emit('planning')
  } finally {
    executingToday.value = false
  }
}

async function loadHome() {
  const data = await api.honmaruHome(props.server)
  profile.value = { ...emptyProfile(), ...data.profile }
  notes.value = data.notes
  homeReady.value = true
}
async function loadSummaries() {
  const jobs = isJp.value ? [
    { label: '近期记录', run: async () => { runs.value = (await api.dataRuns(12, undefined, undefined, undefined, undefined, 'jp')).items } },
    { label: '本丸收获', run: async () => {
      const from = Date.parse(`${today.value}T00:00:00+08:00`) / 1000
      journalEvents.value = (await api.dataEvents(1000, undefined, from - 6 * 86400, from + 86400, 'jp')).items
    } },
    { label: '刀剑整理', run: async () => { swordDepartures.value = (await api.swordArchive('jp')).sword_departures || [] } },
    { label: '家底', run: async () => {
      const stock = await api.clientInventory('jp')
      inventory.value = { resources: Object.fromEntries(Object.entries(stock.resources).map(([name, reading]) => [name, reading.count])) }
    } },
    { label: '游戏近况', run: async () => { situation.value = (await api.honmaruSituation('jp')).situation } },
  ] : [
    { label: '近期记录', run: async () => { runs.value = (await api.dataRuns(12)).items } },
    { label: '规划', run: async () => { planning.value = await api.planning() } },
    { label: '今日安排', run: async () => { dayPlan.value = await api.dayTimeline() } },
    { label: '本丸收获', run: async () => {
      const from = Date.parse(`${today.value}T00:00:00+08:00`) / 1000
      const data = await api.dataEvents(1000, undefined, from - 6 * 86400, from + 86400)
      journalEvents.value = data.items
    } },
    { label: '刀剑整理', run: async () => { swordDepartures.value = (await api.swordArchive()).sword_departures || [] } },
    { label: '近期活动', run: async () => { timeline.value = await api.eventsTimeline() } },
    { label: '家底', run: async () => { inventory.value = (await api.dashboard()).inventory } },
    { label: '游戏近况', run: async () => { situation.value = (await api.honmaruSituation()).situation } },
  ]
  const results = await Promise.allSettled(jobs.map(job => job.run()))
  return results.flatMap((result, index) => result.status === 'rejected' ? [jobs[index]!.label] : [])
}
async function refresh() {
  if (loading.value && homeReady.value) return
  loading.value = true
  const results = await Promise.allSettled([loadHome(), loadSummaries()])
  loadErrors.value = [
    ...(results[0]!.status === 'rejected' ? ['个人档案和小记'] : []),
    ...(results[1]!.status === 'fulfilled' ? results[1]!.value : ['本丸近况']),
  ]
  loading.value = false
}
function editProfile() {
  draft.value = { ...profile.value }
  formError.value = ''
  notice.value = ''
  editingProfile.value = true
}
async function chooseAvatar(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  formError.value = ''
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 512 * 1024) {
    formError.value = '请选择 512 KB 以内的 PNG、JPG 或 WebP 头像。'
    input.value = ''
    return
  }
  try {
    draft.value.avatar = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = () => reject(new Error('头像没能读到，请重新选择。'))
      reader.readAsDataURL(file)
    })
  } catch (error) { formError.value = errorMessage(error) }
}
async function saveProfile() {
  savingProfile.value = true
  formError.value = ''
  try {
    profile.value = (await api.saveHonmaruProfile(draft.value, props.server)).profile
    editingProfile.value = false
    notice.value = '档案收好了。'
  } catch (error) { formError.value = errorMessage(error) }
  finally { savingProfile.value = false }
}
function writeNote(note?: HonmaruNote) {
  noteId.value = note?.id
  noteBody.value = note?.body || ''
  formError.value = ''
  notice.value = ''
  writing.value = true
}
async function saveNote() {
  savingNote.value = true
  formError.value = ''
  try {
    const { note } = await api.saveHonmaruNote(noteBody.value, noteId.value, props.server)
    const index = notes.value.findIndex(item => item.id === note.id)
    if (index >= 0) notes.value[index] = note
    else notes.value.unshift(note)
    writing.value = false
    notice.value = '今天的这一笔，记下了。'
  } catch (error) { formError.value = errorMessage(error) }
  finally { savingNote.value = false }
}
onMounted(() => {
  void refresh()
  timer = window.setInterval(() => { now.value = Date.now() }, 1000)
  dayPlanTimer = window.setInterval(refreshDayPlan, 30000)
})
onBeforeUnmount(() => { window.clearInterval(timer); window.clearInterval(dayPlanTimer) })
watch(() => props.busy, (busy, previous) => { if (previous && !busy) void refresh() })
</script>

<template>
  <div class="honmaru-home">
    <p v-if="loadErrors.length" class="home-load-error" role="alert">{{ loadErrors.join('、') }}暂时没读到。<button type="button" :disabled="loading" @click="refresh">重新读取</button></p>
    <p v-if="notice" class="home-notice" role="status">{{ notice }}</p>
    <aside class="honmaru-profile" aria-label="审神者档案">
      <div class="profile-portrait"><img v-if="profile.avatar" :src="profile.avatar" alt="我的头像"><span v-else aria-hidden="true">{{ (profile.saniwa_name || profile.honmaru_name || '丸').slice(0, 1) }}</span></div>
      <h1>{{ homeName }}</h1>
      <p class="profile-motto">{{ profile.motto || '留一句喜欢的话，给每次回来的自己。' }}</p>
      <div v-if="daysTogether" class="profile-anniversary"><small>就任第</small><strong>{{ daysTogether }}<span> 天</span></strong></div>
      <dl class="profile-facts">
        <div><dt>审神者</dt><dd>{{ profile.saniwa_name || '还没留名' }}</dd></div>
        <div><dt>属国</dt><dd>{{ profile.province || '待填写' }}</dd></div>
        <div><dt>近侍</dt><dd :title="situation?.secretary.observed_at ? situationTime(situation.secretary.observed_at) : ''">{{ situation?.secretary.name || profile.attendant || '待同步' }}</dd></div>
        <div><dt>就任日</dt><dd>{{ profile.joined_on?.replaceAll('-', '.') || '待填写' }}</dd></div>
      </dl>
      <button type="button" class="home-text-button profile-edit" :disabled="!homeReady" @click="editProfile">{{ profile.saniwa_name ? '整理我的档案' : '写下我的档案' }} <span aria-hidden="true">↗</span></button>
    </aside>

    <section class="honmaru-journal" aria-label="本丸动态">
      <header class="journal-heading"><div><p class="home-eyebrow">{{ todayLabel }}</p><h2>{{ welcome }}</h2></div><button v-if="isJp" type="button" class="home-primary" :disabled="launchingGame" @click="launchGame">{{ launchingGame ? '启动中…' : '启动游戏' }}</button><button v-else type="button" class="home-primary" :disabled="!homeReady" @click="writeNote()">＋ 写小记</button></header>
      <div class="home-office-link"><div><span class="office-dot" :class="{ active }"></span><p><strong>{{ active ? currentActivityTitle : isJp ? '日服本丸 · 数据自动更新' : 'まあ丸待命中' }}</strong><small v-if="active && currentActivityStep">{{ currentActivityStep }}</small></p></div><div class="home-office-actions"><button v-if="!active && !isJp" type="button" class="home-primary" :disabled="executingToday || !dayPlan?.conductor.available" @click="executeToday">{{ executingToday ? '正在接班…' : '一键执行今日安排' }}</button><button v-if="active" type="button" class="home-text-button" @click="emit('office')">去执务台 →</button></div></div>
      <div v-for="block in interruptedRaids" :key="block.run_id" class="home-raid-reminder" role="status">
        <span>联队战中断了 · 已完成 {{ block.recovery!.completed }}/{{ block.runs }} 圈</span>
        <button type="button" class="home-text-button" @click="emit('resumeRaid', block.run_id!)">去继续 →</button>
      </div>
      <p v-if="todayExecutionMessage" class="home-notice" role="status">{{ todayExecutionMessage }}</p>
      <div class="journal-filter" aria-label="记录筛选"><button type="button" :class="{ selected: filter === 'all' }" :aria-pressed="filter === 'all'" @click="filter = 'all'; limit = 8">本丸动态</button><button type="button" :class="{ selected: filter === 'notes' }" :aria-pressed="filter === 'notes'" @click="filter = 'notes'; limit = 8">我的小记 <span>{{ notes.length }}</span></button><button type="button" class="journal-refresh" :disabled="loading" @click="refresh">{{ loading ? '整理中…' : '刷新' }}</button></div>
      <button v-if="isJp && filter === 'notes'" type="button" class="home-text-button" :disabled="!homeReady" @click="writeNote()">＋ 写小记</button>
      <div v-if="!entries.length" class="journal-empty"><span aria-hidden="true">✿</span><h3>{{ loading ? '正在翻看本丸记录…' : '日子还长，慢慢记。' }}</h3><p>{{ filter === 'notes' ? '今天的碎念、喜欢的一刻，都可以写在这里。' : '你写下的小记和最近的执务记录，会按日期留在这里。' }}</p><button v-if="!loading" type="button" class="home-text-button" :disabled="!homeReady" @click="writeNote()">写下第一笔 →</button></div>
      <section v-for="group in groups" :key="group.date" class="journal-day">
        <h3 class="journal-date">{{ dateLabel(group.date) }}<span v-if="group.date === today">{{ today.replaceAll('-', '.') }}</span></h3>
        <article v-for="entry in group.entries" :key="entry.key" class="journal-entry" :class="{ 'personal-entry': entry.note, 'attention-entry': entry.run?.status === 'failed', 'new-sword-entry': entry.post?.firstObtained }">
          <template v-if="entry.note"><header class="entry-head"><span class="entry-avatar personal-avatar"><img v-if="profile.avatar" :src="profile.avatar" alt=""><span v-else aria-hidden="true">{{ (profile.saniwa_name || '审').slice(0, 1) }}</span></span><span class="entry-identity"><strong>{{ profile.saniwa_name || '审神者' }}</strong><small>我的小记 · <time>{{ eventTime(entry.ts) }}</time></small></span><button type="button" class="home-text-button" @click="writeNote(entry.note)">修改</button></header><p class="entry-body">{{ entry.note.body }}</p><small v-if="entry.note.updated_at" class="entry-updated">修改于 {{ eventTime(entry.note.updated_at) }}</small></template>
          <template v-else-if="entry.post"><header class="entry-head"><span class="entry-avatar" :class="entry.post.avatar ? 'author-avatar' : 'fox-avatar'"><img :src="entry.post.avatar || '/static/img/fox_frames/v2/idle/transparent/frame_01.png'" @error.once="($event.target as HTMLImageElement).src = '/static/img/ui/' + entry.post.icon" alt=""></span><span class="entry-identity"><strong>{{ entry.post.author }}</strong><small>{{ entry.post.label }} · <time>{{ eventTime(entry.ts) }}</time></small></span></header><div class="entry-story"><h4>{{ entry.post.title }}</h4><p>{{ entry.post.text }}</p><div v-if="entry.post.facts.length" class="entry-facts"><span v-for="fact in entry.post.facts" :key="fact">{{ fact }}</span></div></div><footer v-if="entry.post.label !== '刀剑手记'"><button type="button" class="home-text-button" @click="emit('records')">去账房翻记录 →</button></footer></template>
          <template v-else><header class="entry-head"><span class="entry-avatar fox-avatar"><img :src="'/static/img/fox_frames/v2/idle/transparent/frame_01.png'" alt=""></span><span class="entry-identity"><strong>狐之助</strong><small>{{ runPostKind(entry.run).label }} · <time>{{ eventTime(entry.ts) }}</time></small></span><span class="entry-status" :class="{ 'needs-attention': entry.run.status === 'failed' }">{{ runStatusLabel(entry.run) }}</span></header><div class="entry-story"><h4>{{ runTitle(entry.run) }}</h4><p>{{ runPostText(entry.run) }}</p><div v-if="runPostFacts(entry.run).length" class="entry-facts"><span v-for="fact in runPostFacts(entry.run)" :key="fact">{{ fact }}</span></div></div><footer><button type="button" class="home-text-button" @click="emit('records')">翻开这趟记录 →</button></footer></template>
        </article>
      </section>
      <button v-if="entries.length > limit" class="journal-more home-text-button" type="button" @click="limit += 12">再翻一些记录 ↓</button>
      <button v-if="runs.length && filter === 'all'" class="journal-more home-text-button" type="button" @click="emit('records')">去账房翻更早的记录 →</button>
    </section>

    <aside class="honmaru-keepsakes" aria-label="小报与账房">
      <HonmaruClock :timeline="dayPlan" :now="now" :situation="isJp ? situation : null" :read-only="isJp" @open="isJp ? emit('report') : emit('planning')" />
      <PaperCard variant="dashboard" class="home-brief" aria-label="本丸近况">
        <header class="brief-meta"><span>狐之助小报</span><button type="button" class="home-text-button" :disabled="syncingSituation || active" @click="syncSituation">{{ syncingSituation ? '读取中…' : '同步近况' }}</button></header>
        <p v-if="situationError" class="home-load-error" role="alert">{{ situationError }}</p>
        <p v-if="isJp && situation" class="home-muted">近况读取于 {{ situation.parties_observed_at || situation.secretary.observed_at || '时间待确认' }}</p>
        <p v-if="!situation" class="home-muted">{{ isJp ? '打开日服浏览器，进入本丸后更新近况。' : '进入本丸后，同步近况。' }}</p>
        <div v-else-if="situationMoments.length" class="situation-moments">
          <div v-for="moment in situationMoments" :key="moment.key" class="situation-moment" :title="`${gameTime(moment.time)} · ${situationTime(moment.observedAt)}`"><strong>{{ moment.label }}</strong><span :class="{ 'moment-done': moment.done }">{{ remainingTime(moment.time, now) }}</span></div>
        </div>
        <p v-else class="home-muted">已同步记录中没有远征、锻刀或手入倒计时。</p>
        <HonmaruStatus v-if="isJp" :situation="situation" :now="now" />
      </PaperCard>
      <section class="home-planning-card home-event-card">
        <p class="home-eyebrow">近期活动</p>
        <template v-if="nearestEvent">
          <h2><span aria-hidden="true">⚑</span> {{ nearestEvent.name }}</h2>
          <dl class="planning-rows"><div><dt>{{ eventMomentLabel(nearestEvent) }}</dt><dd><span class="pencil-mark">{{ eventMoment(nearestEvent) }}</span></dd></div><div><dt>预算</dt><dd :class="{ 'budget-ready': nearestEvent.budget?.sufficient === true }">{{ eventBudget(nearestEvent) }}</dd></div><div v-if="nearestEvent.budget?.tama_current != null"><dt>{{ nearestEvent.budget.currency || '活动点数' }}</dt><dd>{{ fmt(nearestEvent.budget.tama_current) }}{{ nearestEvent.budget.tama_target != null ? ` / ${fmt(nearestEvent.budget.tama_target)}` : '' }}</dd></div><div v-if="activityPlan"><dt>今天完成</dt><dd>{{ activityPlan.completed_today }} 圈</dd></div><div v-if="activityPlan"><dt>接下来建议</dt><dd>{{ activityPlan.target_runs }} 圈</dd></div></dl>
        </template>
        <template v-else><h2><span aria-hidden="true">⚑</span> {{ isJp ? '日服活动日程待同步' : '暂无近期活动' }}</h2><p class="planning-note">{{ isJp ? '这里只展示日服日程，不套用国服活动安排。' : '有新日程时，会在这里提醒你。' }}</p></template>
        <button type="button" class="home-text-button" @click="isJp ? emit('report') : emit('planning')">{{ isJp ? '去账房查看 →' : '去规划查看 →' }}</button>
      </section>
      <section class="home-planning-card home-finances">
        <h2>家底</h2>
        <p class="finance-lead">小判 <strong>{{ fmt(recentKoban) }}</strong></p>
        <dl class="finance-resources"><div v-for="name in ['木炭', '玉钢', '冷却材', '砥石']" :key="name"><dt>{{ name }}</dt><dd>{{ resource(name) }}</dd></div></dl>
        <details class="finance-details"><summary>资源与预留</summary><dl><div v-for="name in resourceNames.filter(name => !['小判', '木炭', '玉钢', '冷却材', '砥石'].includes(name))" :key="name"><dt>{{ name === '加速符' && !isJp ? '加速符·极' : name }}</dt><dd>{{ resource(name) }}</dd></div></dl><p v-if="kobanWatch?.reserved" class="planning-note">预留 {{ fmt(kobanWatch.reserved) }} 小判 · 可安排 {{ fmt(kobanWatch.available) }}</p><p v-if="resourceWatch?.forge_capacity != null" class="finance-forge">普通锻刀可锻 {{ fmt(resourceWatch.forge_capacity) }} 炉</p></details>
        <div class="finance-links"><button type="button" class="home-text-button" @click="emit('report')">去账房 →</button></div>
      </section>
    </aside>

    <dialog v-if="editingProfile || writing" ref="editor" class="home-dialog-shell" :aria-label="editingProfile ? '整理我的档案' : '写小记'" @cancel.prevent="!savingProfile && !savingNote && (editingProfile = writing = false)">
      <section class="home-dialog">
        <header><h2>{{ editingProfile ? '整理我的档案' : noteId ? '修改小记' : '写一则小记' }}</h2><button type="button" aria-label="关闭" :disabled="savingProfile || savingNote" @click="editingProfile = writing = false">×</button></header>
        <form v-if="editingProfile" @submit.prevent="saveProfile"><fieldset :disabled="savingProfile"><label class="avatar-picker"><img v-if="draft.avatar" :src="draft.avatar" alt="头像预览"><span>选一张自己的头像<small>PNG / JPG / WebP，512 KB 以内</small></span><input type="file" accept="image/png,image/jpeg,image/webp" @change="chooseAvatar"></label><div class="profile-fields"><label>本丸名<input v-model="draft.honmaru_name" maxlength="40" placeholder="给这里起个名字"></label><label>审神者<input v-model="draft.saniwa_name" maxlength="40" placeholder="你的名字"></label><label>属国<input v-model="draft.province" maxlength="30" placeholder="例如：备前国"></label><label>就任日<input v-model="draft.joined_on" type="date" :max="today"></label><label class="field-wide">一言<textarea v-model="draft.motto" maxlength="120" rows="2" placeholder="写一句自己喜欢的话"></textarea></label></div></fieldset><p v-if="formError" role="alert" class="home-form-error">{{ formError }}</p><footer><button type="button" class="home-text-button" :disabled="savingProfile" @click="editingProfile = false">先不改了</button><button type="submit" class="home-primary" :disabled="savingProfile">{{ savingProfile ? '收好中…' : '收好档案' }}</button></footer></form>
        <form v-else @submit.prevent="saveNote"><label class="note-label">今天想记住什么？<textarea v-model="noteBody" rows="7" maxlength="2000" required :disabled="savingNote" placeholder="一点碎念，一件小事，或者今天终于等到的那个人。"></textarea></label><small>{{ noteBody.length }} / 2000</small><p v-if="formError" role="alert" class="home-form-error">{{ formError }}</p><footer><button type="button" class="home-text-button" :disabled="savingNote" @click="writing = false">先不写了</button><button type="submit" class="home-primary" :disabled="savingNote || !noteBody.trim()">{{ savingNote ? '记录中…' : '记在本丸里' }}</button></footer></form>
      </section>
    </dialog>
  </div>
</template>

<style scoped>
.honmaru-home { --home-green: #536d55; display: grid; grid-template-columns: 210px minmax(0, 1fr) 230px; gap: 30px; align-items: start; color: var(--ink); max-width: 1300px; margin: auto; }
.honmaru-home h1, .honmaru-home h2, .honmaru-home h3, .honmaru-home h4, .honmaru-home p { margin: 0; }
.honmaru-home button { transition: background .15s, color .15s; }
.honmaru-home button:disabled { cursor: default; opacity: .55; }
.honmaru-home :is(button, input, textarea):focus-visible { outline: 2px solid var(--home-green); outline-offset: 3px; }
.honmaru-home .home-eyebrow { color: var(--ink-dim); font-size: 11px; letter-spacing: .1em; margin-bottom: 10px; }
.home-text-button { color: var(--home-green); border: 0; background: transparent; padding: 4px 0; font-size: 12px; text-align: left; }
.home-text-button:hover { color: var(--ink); text-decoration: underline; }
.home-primary { padding: 9px 16px; border: 1px solid var(--home-green); border-radius: 5px; color: #fffaf0; background: var(--home-green); font-weight: 600; white-space: nowrap; }
.home-primary:hover { background: #405844; }
.honmaru-profile { padding: 5px 22px 22px 0; border-right: 1px solid var(--paper-line); overflow-wrap: anywhere; }
.profile-portrait { display: grid; place-items: center; width: 88px; height: 100px; padding: 6px 6px 16px; margin-bottom: 22px; border: 1px solid #c8bda6; background: #fbf8ee; box-shadow: 3px 4px 0 #d9ceba; transform: rotate(-3deg); }
.profile-portrait img { width: 100%; height: 100%; object-fit: cover; }
.profile-portrait > span { display: grid; place-items: center; width: 100%; height: 100%; font: 36px Georgia, 'Microsoft YaHei', serif; color: #65735d; background: #e2e7d6; }
.honmaru-profile h1 { font-size: 24px; line-height: 1.4; margin-bottom: 12px; }
.honmaru-profile .profile-motto { color: #716452; font-size: 13px; line-height: 1.9; white-space: pre-wrap; }
.profile-anniversary { display: grid; border-top: 1px solid var(--paper-line); margin-top: 23px; padding-top: 18px; }
.profile-anniversary small { color: var(--ink-dim); font-size: 11px; }
.profile-anniversary strong { color: var(--home-green); font: 32px Georgia, serif; }
.profile-anniversary strong span { font-size: 12px; font-family: inherit; }
.profile-facts { margin: 24px 0 16px; display: grid; gap: 13px; font-size: 12px; }
.profile-facts div { display: grid; grid-template-columns: 54px minmax(0, 1fr); gap: 8px; }
.profile-facts dt { color: var(--ink-dim); }
.profile-facts dd { margin: 0; }
.profile-edit { width: 100%; border-top: 1px dashed #c8bda6; padding-top: 15px; display: flex; justify-content: space-between; }
.honmaru-profile .profile-footnote { color: var(--ink-dim); font-size: 11px; margin-top: 32px; line-height: 1.9; }
.honmaru-journal { min-width: 0; }
.journal-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding-bottom: 22px; }
.journal-heading h2 { font-size: 22px; line-height: 1.5; overflow-wrap: anywhere; }
.journal-heading div > p:last-child { color: var(--ink-dim); margin-top: 5px; font-size: 12px; }
.home-office-link { display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 12px 15px; background: #e8ecdf; border-left: 3px solid #a7b79b; margin-bottom: 24px; }
.home-office-link > div { display: flex; align-items: center; gap: 10px; min-width: 0; }
.home-office-link p { display: grid; gap: 3px; overflow-wrap: anywhere; }
.home-office-link strong { font-weight: 500; font-size: 12px; }
.home-office-link small { color: var(--ink-dim); font-size: 11px; }
.home-office-link button { flex-shrink: 0; }
.office-dot { width: 7px; height: 7px; background: #8ea082; border-radius: 50%; flex-shrink: 0; }
.office-dot.active { background: #c99430; }
.journal-filter { display: flex; align-items: center; gap: 22px; border-bottom: 1px solid var(--paper-line); }
.journal-filter button { padding: 0 0 11px; background: transparent; color: var(--ink-dim); border: 0; border-bottom: 2px solid transparent; font-size: 13px; }
.journal-filter button.selected { color: var(--ink); border-color: var(--home-green); font-weight: 600; }
.journal-filter span { font-size: 10px; margin-left: 5px; }
.journal-filter .journal-refresh { margin-left: auto; font-size: 11px; }
.journal-day { margin-top: 24px; }
.honmaru-home .journal-date { font-size: 13px; display: flex; align-items: center; gap: 10px; margin-bottom: 13px; color: var(--home-green); }
.journal-date span { font-size: 10px; color: var(--ink-dim); font-weight: 400; }
.journal-entry { padding: 15px 17px; margin-bottom: 16px; border: 1px solid var(--paper-line); background: var(--paper-card); border-radius: var(--r-md); box-shadow: 0 3px 12px #3d32290b; overflow: hidden; }
.journal-entry.attention-entry { border-color: #d7aa9d; }
.journal-entry.new-sword-entry { border-color: #c9a75d; }
.author-avatar img { object-position: center; }
.journal-entry.personal-entry { position: relative; border-left: 3px solid #c6ae76; box-shadow: 0 2px 3px #3d322908; clip-path: polygon(0 0, calc(100% - 11px) 0, 100% 11px, 100% calc(100% - 3px), 97% 100%, 93% calc(100% - 2px), 88% 100%, 82% calc(100% - 2px), 76% 100%, 69% calc(100% - 2px), 62% 100%, 54% calc(100% - 2px), 47% 100%, 39% calc(100% - 2px), 31% 100%, 23% calc(100% - 2px), 15% 100%, 8% calc(100% - 2px), 0 100%); }
.journal-entry.personal-entry::after { content: ''; position: absolute; top: 0; right: 0; width: 11px; height: 11px; background: linear-gradient(225deg, var(--paper) 0 47%, #c9bda7 50% 57%, #eee6d5 60%); }
.journal-entry.personal-entry header button { margin-right: 8px; }
.journal-entry .entry-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.entry-head > button, .entry-status { margin-left: auto; }
.entry-avatar { flex: 0 0 36px; display: grid; place-items: center; width: 36px; height: 36px; overflow: hidden; border-radius: 50%; background: #e8ecdf; }
.entry-avatar img { width: 100%; height: 100%; object-fit: cover; }
.fox-avatar img { width: 42px; height: 42px; object-fit: cover; object-position: center 46%; image-rendering: pixelated; }
.personal-avatar { color: #5e6d57; font-size: 16px; font-weight: 700; }
.entry-identity { display: grid; min-width: 0; gap: 2px; }
.entry-identity strong { font-size: 12px; font-weight: 600; }
.entry-identity small { color: var(--ink-dim); font-size: 10px; }
.entry-scene { position: relative; height: 105px; margin: 0 0 13px; border-radius: 5px; background-color: #e7dfcc; background-position: center 62%; background-size: cover; }
.scene-stamp { position: absolute; right: 10px; bottom: -11px; display: grid; place-items: center; width: 32px; height: 32px; border: 2px solid var(--paper-card); border-radius: 50%; background: #e8ecdf; box-shadow: 0 2px 5px #3d32292b; }
.scene-stamp img { width: 19px; height: 19px; object-fit: contain; }
.entry-story { padding: 0 1px; }
.entry-story p { margin-top: 7px; color: var(--ink-dim); font-size: 12px; line-height: 1.7; }
.entry-facts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 11px; }
.entry-facts span { padding: 4px 8px; color: var(--home-green); background: #e9eee5; border-radius: 4px; font-size: 10px; }
.journal-entry footer { margin-top: 13px; padding-top: 7px; border-top: 1px solid var(--paper-line); }
.honmaru-home .entry-body { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; line-height: 1.9; }
.journal-entry h4 { font-size: 15px; font-weight: 600; }
.entry-updated { display: block; color: var(--ink-dim); font-size: 10px; margin-top: 12px; }
.entry-status.needs-attention { color: #a03f32; }
.journal-more { display: block; margin: 18px auto; text-align: center; }
.journal-empty { padding: 50px 20px; text-align: center; }
.journal-empty > span { color: #afbaa2; font-size: 34px; }
.journal-empty h3 { margin: 14px 0 10px; font-size: 17px; font-weight: 500; }
.journal-empty p { color: var(--ink-dim); line-height: 1.9; font-size: 12px; margin-bottom: 18px; }
.honmaru-keepsakes { display: grid; gap: 25px; min-width: 0; }
.home-situation { padding: 16px; border: 1px solid var(--paper-line); background: var(--paper-card); border-radius: 12px; min-width: 0; }
.home-situation header { display: flex; align-items: start; justify-content: space-between; gap: 10px; }
.home-situation header h2 { margin: 2px 0 12px; }
.home-situation .home-text-button { white-space: nowrap; }
.situation-caption small, .situation-moment small { color: var(--ink-dim); font-size: 10px; }
.situation-moments { border-top: 1px solid var(--paper-line); padding-top: 10px; }
.situation-caption { display: flex; justify-content: space-between; gap: 8px; margin: 0 0 8px; font-size: 11px; color: var(--ink-dim); }
.situation-moment { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 8px; padding: 7px 0; font-size: 12px; }
.situation-moment strong { font-weight: 600; }
.situation-moment span { text-align: right; font-variant-numeric: tabular-nums; }
.situation-moment small { grid-column: 1 / -1; }
.situation-more { margin: 7px 0 0; color: var(--ink-dim); font-size: 10px; }
.situation-details, .finance-details { margin-top: 12px; border-top: 1px solid var(--paper-line); padding-top: 10px; }
.situation-details summary, .finance-details summary { cursor: pointer; color: var(--home-green); font-size: 11px; }
.situation-detail-group { margin-top: 12px; }
.situation-detail-group > p { margin: 0; color: var(--ink-dim); font-size: 11px; }
.situation-row { display: grid; gap: 3px; margin-top: 8px; font-size: 11px; }
.situation-row span, .situation-row small { color: var(--ink-dim); }
.situation-row strong { font-weight: 500; overflow-wrap: anywhere; }
.situation-members { display: flex; flex-wrap: wrap; gap: 4px 10px; }
.situation-member small { color: var(--ink-dim); font-weight: 400; }
.situation-moment .moment-done { color: #315f42; font-weight: 600; }
.finance-details-caption { margin: 10px 0 0; color: var(--ink-dim); font-size: 10px; }
.honmaru-keepsakes h2 { font-size: 15px; margin-bottom: 12px; }
.home-brief { position: relative; padding: 20px 18px 17px; border: 1px solid #e3d5b7; background: #f3ecd9; border-radius: var(--r-md); box-shadow: 2px 3px 0 #e5dac4; }
.home-brief::before { content: ''; width: 45px; height: 13px; position: absolute; top: -6px; left: calc(50% - 22px); background: #d3c79a88; transform: rotate(-4deg); }
.brief-meta { display: flex; justify-content: space-between; gap: 8px; margin-bottom: 12px; color: #806748; font-size: 10px; }
.home-brief h2 { color: #173d6e; font-weight: 500; }
.home-daily-fact { margin-top: 10px !important; font-size: 12px; line-height: 1.6; }
.home-daily-fact small { color: var(--ink-dim); overflow-wrap: anywhere; }
.brief-detail { color: #173d6e; font-size: 12px; line-height: 1.65; }
.brief-changes { display: flex; flex-wrap: wrap; gap: 5px 10px; margin: 9px 0; padding: 8px 10px; color: #53635b; background: linear-gradient(105deg, #edf0e7d9, #dce4dbb8 52%, #f4f3e9c7); border-block: 1px solid #ffffff75; box-shadow: inset 0 1px 4px #fff8, 0 1px 2px #625a4b14; backdrop-filter: blur(.6px); font-size: 10px; }
.brief-changes span { white-space: nowrap; }
.brief-changes b { font-weight: 500; }
.honmaru-home .home-muted { color: #796e5f; font-size: 12px; line-height: 1.8; margin: 6px 0 12px; }
.home-planning-card { padding: 17px 16px; border: 1px solid var(--paper-line); background: var(--paper-card); }
.home-finances { padding: 0 0 20px; border: 0; border-bottom: 1px solid var(--paper-line); background: transparent; }
.finance-resources { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 16px; margin: 16px 0; }
.finance-resources dt { color: var(--ink-dim); font-size: 11px; }
.finance-resources dd { margin: 3px 0 0; font-size: 15px; font-variant-numeric: tabular-nums; }
.home-brief .situation-moments { border-top: 0; padding-top: 0; }
.home-brief .situation-moment { grid-template-columns: minmax(0, 1fr) auto; padding-block: 9px; border-bottom: 1px dashed #d6c9aa; }
.home-brief .situation-moment:last-child { border-bottom: 0; }
.home-brief .brief-meta { align-items: center; }
.honmaru-keepsakes .home-planning-card h2 { margin: 0 0 12px; color: #173d6e; font-size: 15px; font-weight: 500; }
.home-planning-card .home-eyebrow { margin-bottom: 5px; color: #a87416; }
.pencil-mark { position: relative; z-index: 0; display: inline-block; padding-inline: 2px; font-weight: inherit; }
.pencil-mark::after { content: ''; position: absolute; z-index: -1; left: -1px; right: 1px; bottom: 0; height: 3px; background: linear-gradient(177deg, transparent 18%, #756c6090 31% 55%, transparent 69%); transform: rotate(-1.2deg); }
.honmaru-home .planning-note { margin: 10px 0 8px; color: #796e5f; font-size: 11px; line-height: 1.7; }
.hakata-mark { margin-right: 4px; color: #173d6e; font-size: 11px; }
.finance-lead { display: grid; gap: 2px; margin: 3px 0 8px; color: var(--ink-dim); font-size: 11px; }
.finance-lead strong { color: #173d6e; font: 24px/1.2 Georgia, serif; font-variant-numeric: tabular-nums; }
.finance-forge { margin: 12px 0 0; padding-top: 10px; border-top: 1px solid var(--paper-line); color: #173d6e; font-size: 11px; line-height: 1.6; }
.finance-details dl { display: grid; gap: 5px; margin: 11px 0 0; }
.finance-details dl > div { display: flex; justify-content: space-between; gap: 8px; font-size: 11px; }
.finance-details dt { color: var(--ink-dim); }
.finance-details dd { margin: 0; font-variant-numeric: tabular-nums; }
.finance-links { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; margin-top: 13px; }
.planning-rows { margin: 0; border-top: 1px solid #ded6c7; }
.planning-rows div { display: flex; justify-content: space-between; gap: 10px; padding: 7px 0; border-bottom: 1px solid #ded6c7; font-size: 12px; }
.planning-rows dt { color: #173d6e; }
.planning-rows dd { margin: 0; color: #173d6e; font-variant-numeric: tabular-nums; text-align: right; }
.planning-rows dd.budget-ready { display: inline-flex; align-items: center; gap: 5px; color: #315f42; font-weight: 600; }
.planning-rows dd.budget-ready::before { content: '备'; display: grid; width: 18px; height: 18px; place-items: center; color: #a34535; border: 1px solid #a34535; border-radius: 50%; box-shadow: inset 0 0 0 1px #a3453540; font: 9px/1 serif; transform: rotate(-8deg); }
.home-load-error, .home-notice { grid-column: 1 / -1; padding: 10px 14px; background: #f2e2cd; font-size: 12px; }
.home-load-error button { background: transparent; border: 0; text-decoration: underline; margin-left: 12px; color: inherit; }
.home-notice { background: #e8ecdf; }
.home-dialog-shell { width: min(510px, calc(100% - 24px)); max-height: 90dvh; padding: 0; border: 0; border-radius: 12px; background: var(--paper-card); box-shadow: var(--shadow-pop); }
.home-dialog-shell::backdrop { background: #30291f77; }
.home-dialog { padding: 25px; color: var(--ink); border: 1px solid #c8bda6; border-radius: 12px; }
.home-dialog > header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 23px; }
.home-dialog h2 { font-size: 19px; }
.home-dialog > header button { color: var(--ink); background: transparent; border: 0; font-size: 24px; padding: 0 5px; }
.home-dialog fieldset { border: 0; padding: 0; margin: 0; min-width: 0; }
.profile-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
.profile-fields label, .note-label { display: grid; gap: 6px; font-size: 12px; }
.field-wide { grid-column: 1 / -1; }
.home-dialog input:not([type='file']), .home-dialog textarea { width: 100%; min-width: 0; padding: 10px; color: var(--ink); background: var(--paper); border: 1px solid var(--paper-line); border-radius: var(--r-sm); }
.home-dialog textarea { resize: vertical; }
.home-dialog footer { display: flex; justify-content: flex-end; gap: 20px; margin-top: 22px; }
.home-dialog form > small { display: block; color: var(--ink-dim); text-align: right; margin-top: 5px; }
.home-dialog .home-form-error { color: #9b392b; margin-top: 15px; font-size: 12px; }
.avatar-picker { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; padding-bottom: 20px; font-size: 13px; }
.avatar-picker img { width: 54px; height: 54px; object-fit: cover; border: 3px solid #e5dac4; }
.avatar-picker small { display: block; color: var(--ink-dim); font-size: 10px; }
.avatar-picker input { font-size: 12px; width: 100%; }
@media (max-width: 1100px) { .honmaru-home { grid-template-columns: 175px minmax(0, 1fr); gap: 25px; } .honmaru-keepsakes { grid-column: 2; grid-template-columns: 1fr 1fr; gap: 20px; } }
@media (max-width: 720px) { .honmaru-home { grid-template-columns: 1fr; gap: 25px; } .honmaru-profile { padding: 0 0 20px; border-right: 0; border-bottom: 1px solid var(--paper-line); display: grid; grid-template-columns: 66px minmax(0, 1fr); column-gap: 20px; } .profile-portrait { grid-row: 1 / 4; width: 66px; height: 74px; padding: 4px 4px 11px; margin: 3px 0 0; } .honmaru-profile .home-eyebrow { margin-bottom: 4px; } .honmaru-profile h1 { font-size: 21px; margin-bottom: 6px; } .profile-motto { grid-column: 2; } .profile-facts { grid-column: 1 / -1; grid-template-columns: 1fr 1fr; margin: 20px 0 12px; gap: 12px; } .profile-anniversary { grid-column: 1 / -1; display: flex; align-items: baseline; gap: 12px; margin-top: 16px; padding-top: 12px; } .profile-anniversary strong { font-size: 24px; } .profile-edit { grid-column: 1 / -1; } .profile-footnote { display: none; } .journal-heading h2 { font-size: 20px; } .journal-heading { gap: 10px; } .home-primary { padding: 8px 12px; font-size: 12px; } .honmaru-keepsakes { grid-column: 1; grid-template-columns: 1fr; } .home-dialog { padding: 20px; } .home-dialog-backdrop { padding: 12px; } .journal-entry { padding: 14px; } }
@media (prefers-reduced-motion: reduce) { .honmaru-home button { transition: none; } }
.home-office-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.home-office-link .home-office-actions { flex-shrink: 0; }
@media (max-width: 600px) { .home-office-link { flex-wrap: wrap; gap: 12px; } .home-office-actions { width: 100%; } }
.home-raid-reminder { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px 16px; margin: -12px 0 20px; padding: 10px 14px; border-left: 3px solid var(--fox-gold); color: var(--ink); background: var(--fox-gold-pale); font-size: 12px; }
.home-raid-reminder button { flex-shrink: 0; }
</style>
