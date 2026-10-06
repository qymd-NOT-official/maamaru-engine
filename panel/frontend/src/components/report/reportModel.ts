// 本丸成绩单共享的展示模型：来源分类、配色、格式化与小助手
import type { LedgerAttribution } from '../../types'

export const resourceNames = ['小判', '木炭', '玉钢', '冷却材', '砥石', '委托符', '加速符', '甲州金']
export function resourceLabel(name: string) { return name === '加速符' ? '加速符·极' : name === '活动点数·10031' ? '夜光贝' : name }

export interface SourceCategory { key: string; label: string; color: string }

// 顺序即柱子堆叠顺序；unknown 永远垫底（视觉上最贴近零线的是它也行，主要是灰色一眼可辨）
export const sourceCategories: SourceCategory[] = [
  { key: 'osaka', label: '大阪城', color: '#d4a017' },
  { key: 'expedition', label: '远征', color: '#7a9e5f' },
  { key: 'forge', label: '锻刀', color: '#b56a4c' },
  { key: 'repair', label: '手入', color: '#6a8caf' },
  { key: 'task_rewards', label: '任务报酬', color: '#9a7bb0' },
  { key: 'yosari', label: '异去', color: '#4fa3a5' },
  { key: 'inbox', label: '收信箱', color: '#b39c67' },
  { key: 'signin', label: '签到', color: '#b38d9e' },
  { key: 'salary', label: '月卡俸禄', color: '#c89a45' },
  { key: 'artifact', label: '宝物道具', color: '#957963' },
  { key: 'other', label: '其他来源', color: '#c7b299' },
  { key: 'human', label: '你补记的', color: '#a89c8d' },
  { key: 'unknown', label: '来源未确认', color: '#ddd6cb' },
]

export const resourceColors: Record<string, string> = {
  小判: '#d4a017', 木炭: '#6a8caf', 玉钢: '#7a9e5f', 冷却材: '#4fa3a5',
  砥石: '#b56a4c', 委托符: '#9a7bb0', 加速符: '#c96f4a', 甲州金: '#8a7f72',
}

export function categoryOf(source: string | undefined): string {
  const head = String(source || '').split(/[./]/)[0]
  return sourceCategories.some(item => item.key === head) ? head : 'other'
}

export function categoryLabel(key: string): string {
  return sourceCategories.find(item => item.key === key)?.label || key
}

export function recordOrigin(item: LedgerAttribution, runs: any[] = []): string {
  if (item.script === 'manual' || item.source.startsWith('human')) return '你补记的'
  const run = runs.find(run => run.run_id === item.run_id)
  const name = item.run_label?.trim() || run?.label?.trim() || scriptNames[item.execution_script || item.script || ''] || ''
  if (item.script === 'youzu_log') {
    return item.execution_script || item.run_label || run?.label ? `游戏记录 · まあ丸执行${name}` : '游戏记录'
  }
  return `まあ丸${name ? ` · ${name}` : ''}`
}

export function linkReceiptRuns(receipts: LedgerAttribution[], events: any[], runs: any[]): LedgerAttribution[] {
  return receipts.map(receipt => {
    if (receipt.run_id) return receipt
    const matches = events.filter(event => event.run_id && event.script !== 'youzu_log'
      && event.event_type === 'resource.change' && event.payload?.resource === receipt.resource
      && event.payload?.delta === receipt.delta && Math.abs(event.ts - receipt.ts) <= 10
      && categoryOf(event.payload?.source) === categoryOf(receipt.source))
    const owners = new Set(matches.map(event => event.run_id))
    if (receipt.resource === '小判' && receipt.delta < 0 && receipt.source.startsWith('ticket.')
      && receipt.source.includes('sally/recovercost')) {
      for (const event of events) if (event.event_type === 'ticket.refilled' && event.run_id
        && event.script !== 'youzu_log' && event.payload?.source
        && Math.abs(event.ts - receipt.ts) <= 10
        && (event.payload.ticket_price == null || event.payload.ticket_price === -receipt.delta)) owners.add(event.run_id)
    }
    if (receipt.resource === '归城提灯五' && receipt.delta < 0 && receipt.source.startsWith('yosari.')) {
      for (const run of runs) if ((run.loop_records || []).some((loop: any) =>
        loop.mode === 'yosari' && loop.started_at <= receipt.ts && receipt.ts <= loop.ended_at)) owners.add(run.run_id)
    }
    if (receipt.resource === '活动点数·10031' && receipt.source.startsWith('raid.')) {
      for (const event of events) if (event.event_type === 'raid.round_completed' && event.run_id
        && event.payload?.shells === receipt.delta && event.ts >= receipt.ts && event.ts - receipt.ts <= 60) owners.add(event.run_id)
    }
    if (owners.size !== 1) return receipt
    const run_id = [...owners][0]
    const refill = receipt.source.startsWith('ticket.') && events.find(event => event.run_id === run_id
      && event.event_type === 'ticket.refilled' && Math.abs(event.ts - receipt.ts) <= 10)
    const activity = refill && scriptNames[String(refill.payload?.source || '').toLowerCase()]
    return { ...receipt, run_id, run_label: runs.find(run => run.run_id === run_id)?.label,
      label: activity ? `${activity}补充手形` : receipt.label }
  })
}

export function gameLedgerRecords(attributions: LedgerAttribution[]) {
  const groups = new Map<string, any>()
  const seen = new Set<string>()
  for (const item of attributions.filter(a => a.script === 'youzu_log')) {
    const identity = `${item.event_id || item.id}:${item.ts}:${item.resource}`
    if (seen.has(identity)) continue
    seen.add(identity)
    const rawLabel = (item.label || '').split(` ${item.resource} `)[0]
    const label = item.source.startsWith('unknown') || !rawLabel || rawLabel.startsWith(item.resource)
      ? '资源变化 · 来源未确认' : rawLabel
    const key = `${item.ts}:${item.source}:${label}:${item.run_id || ''}`
    let group = groups.get(key)
    if (!group) {
      group = { id: `game-${item.event_id || item.id}`, ts: item.ts, run_id: item.run_id,
        event_type: 'game.resource_changed', payload: { label, resources: {}, origin: recordOrigin(item) }, items: [] }
      groups.set(key, group)
    }
    const name = resourceLabel(item.resource)
    group.payload.resources[name] = (group.payload.resources[name] || 0) + item.delta
  }
  return [...groups.values()]
}

export function signed(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  return `${value > 0 ? '+' : ''}${value.toLocaleString()}`
}

const dateFmt = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' })

export function shanghaiDate(ts: number): string {
  return dateFmt.format(new Date(ts * 1000))
}

export function dayRange(date: string): [number, number] {
  const start = new Date(`${date}T00:00:00+08:00`).getTime() / 1000
  return [start, start + 86400]
}

// 日历日翻页：先换成秒再加减整天（直接往毫秒上加 86400 会只挪 86.4 秒）
export function shiftShanghaiDate(date: string, deltaDays: number): string {
  return shanghaiDate(dayRange(date)[0] + deltaDays * 86400)
}

export function dayLabel(date: string): string {
  return String(date || '').slice(5).replace('-', '/')
}

export function eventTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export const scriptNames: Record<string, string> = {
  osaka: '大阪城', edocastle: '江户城潜入调查', sortie: '合战场', yosari: '异去', raid: '联队战',
  pumpkin: '南瓜大作战', daily: '一键日课',
  expedition: '远征', practice: '演练', smith: '锻刀', repair: '手入',
  sakura: '刷花', sugar: '炼糖', rotate_captain: '换队长', scheduler: '排班',
  inbox_supplies: '收杂物箱', snapshot: '库存盘点', workflow: '自定义工作流',
  youzu_log: '游戏记录', game_inventory: '读取游戏家底',
}

// ---- 每轮任务（run）展示助手 ----

export function activityTitle(activity: any): string {
  const label = String(activity?.label || activity?.script || '')
  return scriptNames[label] || label || '本丸正在执务'
}

export function activityStep(activity: any): string {
  const step = String(activity?.step || '').trim()
  if (!step) return ''
  // 步骤中的玩法后缀用于执行，不是面向玩家的标题。
  const name = scriptNames[step.split(':')[0] || '']
  if (name) return name
  return /^[a-zA-Z0-9_:.-]+$/.test(step) ? '任务进行中' : step
}

export function runElapsedSeconds(run: any): number | null {
  const precise = Number(run.play_duration_seconds)
  if (Number.isFinite(precise) && precise >= 0) return precise
  const fallback = Number(run.duration_seconds)
  return Number.isFinite(fallback) && fallback >= 0 ? fallback : null
}

export function elapsedTime(seconds: number | null): string {
  if (seconds == null || seconds < 0) return '用时未记录'
  const minutes = Math.max(0, Math.round(seconds / 60))
  const hours = Math.floor(minutes / 60), rest = minutes % 60
  return hours ? `${hours}小时${rest ? `${rest}分` : ''}` : `${rest}分钟`
}

export function loopTime(seconds: number | null): string {
  if (!seconds) return '圈速积累中'
  const value = Math.round(seconds)
  return `${Math.floor(value / 60)}分${String(value % 60).padStart(2, '0')}秒/圈`
}

export function runTitle(run: any): string {
  const name = (typeof run.label === 'string' && run.label.trim())
    ? run.label
    : (scriptNames[run.script] || '挂机任务')
  const loops = Number(run.loops || 0)
  if (run.script === 'osaka' && run.selected_floor != null) {
    return loops > 0 ? `大阪城 ${run.selected_floor}F · ${loops} 圈` : `大阪城 ${run.selected_floor}F`
  }
  return loops > 0 ? `${name} · ${loops} 圈` : name
}

export function runStatusLabel(run: any): string {
  const labels: Record<string, string> = {
    waiting: '等待继续',
    completed: '已完成', stopped: '已手动停止', failed: '翻车',
  }
  return labels[String(run.status || '')] || String(run.status || '状态未记录')
}

const deltaOrder = ['小判', '木炭', '玉钢', '冷却材', '砥石', '委托符', '加速符', '甲州金']

export function deltaStats(run: any): string {
  return deltaOrder.filter(name => run.resource_delta?.[name])
    .map(name => `${resourceLabel(name)} ${signed(Number(run.resource_delta[name]))}`).join(' · ')
}

export function attributedStats(run: any): string {
  return deltaOrder.filter(name => run.attributed_resource_delta?.[name])
    .map(name => `${resourceLabel(name)} ${signed(Number(run.attributed_resource_delta[name]))}`).join(' · ')
}

export interface DayStack {
  date: string
  total: number | null
  byCategory: Record<string, number>
}

export function kobanPerHour(run: any): number | null {
  const koban = Number(run.resource_delta?.['小判'])
  const seconds = Number(runElapsedSeconds(run))
  return Number.isFinite(koban) && seconds > 0 ? Math.round(koban * 3600 / seconds) : null
}

export function kobanPerHourLabel(run: any): string {
  const value = kobanPerHour(run)
  return value == null ? '' : value.toLocaleString()
}

export function kobanPerFloorLabel(run: any): string {
  const ks = run.koban_session
  if (!ks || !ks.floors) return ''
  const delta = Number(ks.after) - Number(ks.before)
  if (!Number.isFinite(delta)) return ''
  return `${(delta / ks.floors).toFixed(1)} 小判`
}

// 把一天的归因明细按来源分类汇总
export function sumByCategory(attributions: LedgerAttribution[]): Record<string, number> {
  const totals: Record<string, number> = {}
  for (const item of attributions) {
    const key = categoryOf(item.source)
    totals[key] = (totals[key] || 0) + Number(item.delta || 0)
  }
  return totals
}

// 刀剑进账来源的展示名
export function swordReceiptEntries(events: any[]): any[] {
  // 领取是第二阶段；已知同一实例的锻刀/掉落只算一次获得。
  const obtained = new Set(events.filter(event => ['sword.obtained', 'forge.collected'].includes(event.event_type))
    .flatMap(event => event.payload?.swords || [event.payload]).map(p => p?.serial_id).filter(Boolean))
  return events.flatMap(event => Array.isArray(event.payload?.swords)
    ? event.payload.swords.map((sword: any) => ({ ...event, payload: { ...event.payload, ...sword } }))
    : [event]).filter(event =>
    ['sword.obtained', 'forge.collected', 'pumpkin.sword_obtained', 'sword.inbox_received'].includes(event.event_type)
    && !(event.event_type === 'sword.inbox_received' && obtained.has(event.payload?.serial_id))
    && event.payload?.name)
}

export function obtainSourceLabel(source: string | undefined): string {
  if (source === 'inbox.claim') return '收件箱领取'
  if (source === 'battle.drop') return '战斗掉落'
  if (source === 'raid.drop') return '联队战掉落'
  if (source === 'sortie.drop') return '出阵掉落'
  if (source === 'osaka.drop') return '大阪城挖地'
  if (source === 'forge') return '锻刀'
  if (source === 'pumpkin') return '南瓜大作战'
  return source || ''
}

// 收入和支出分别汇总；不同资源不按数量争“第一”。
export function honmaruReceipts(items: LedgerAttribution[], direction: 'gain' | 'cost') {
  return resourceNames.flatMap(resource => {
    const sources = new Map<string, number>()
    for (const item of items) {
      if (item.resource !== resource || !Number.isFinite(item.delta)) continue
      if (direction === 'gain' ? item.delta <= 0 : item.delta >= 0) continue
      const source = categoryOf(item.source)
      if (source === 'unknown') continue
      sources.set(source, (sources.get(source) || 0) + Math.abs(item.delta))
    }
    if (!sources.size) return []
    const total = [...sources.values()].reduce((a, b) => a + b, 0)
    const leader = [...sources].sort((a, b) => b[1] - a[1])[0]
    return [{ resource, total, source: leader[0], sourceAmount: leader[1] }]
  })
}
