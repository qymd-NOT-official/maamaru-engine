import type { DayTimeline, HonmaruSituation } from '../types'

export function gameStamp(value: string) { return Date.parse(value.replace(' ', 'T') + '+08:00') }
const numerals = ['零', '一', '二', '三', '四', '五']
export function homeMoments(state: HonmaruSituation | null, now: number) {
  if (!state) return []
  return [
    ...state.parties.filter(p => p.finished_at).map(p => ({ key: `party-${p.party_no}`, label: `${numerals[p.party_no] || p.party_no}队远征归来`, time: p.finished_at!, observedAt: state.parties_observed_at })),
    ...state.kiwame_return.map((s, i) => ({ key: `return-${i}`, label: `${s.name || '刀剑'}修行归来`, time: s.finished_at, observedAt: state.kiwame_observed_at })),
    ...(state.forge_slots || []).map(s => ({ key: `forge-${s.slot_no}`, label: `${numerals[s.slot_no] || s.slot_no}号炉完成`, time: s.finished_at, observedAt: state.forge_observed_at })),
    ...(state.repair || []).filter(s => s.finished_at).map(s => ({ key: `repair-${s.slot_no}`, label: s.name ? `${s.name}手入完成` : `${numerals[s.slot_no] || s.slot_no}号手入室完成`, time: s.finished_at!, observedAt: state.repair_observed_at ?? null })),
    ...(state.duty?.finished_at ? [{ key: 'duty', label: '内番完成', time: state.duty.finished_at, observedAt: state.duty_observed_at ?? null }] : []),
  ].filter(s => Number.isFinite(gameStamp(s.time)))
    .map(s => ({ ...s, done: gameStamp(s.time) <= now }))
    .sort((a, b) => Number(b.done) - Number(a.done) || gameStamp(a.time) - gameStamp(b.time))
}

export function remainingTime(time: string, now: number) {
  const seconds = Math.ceil((gameStamp(time) - now) / 1000)
  if (!Number.isFinite(seconds)) return '时间待确认'
  if (seconds <= 0) return '预计已完成'
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60)
  return `${hours ? `${hours}:` : ''}${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}

export function situationSegments(state: HonmaruSituation | null, now: number) {
  const day = new Date(now).toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' })
  const start = Date.parse(`${day}T00:00:00+08:00`)
  return homeMoments(state, now).filter(moment => !moment.done).map(moment => ({
    key: moment.key, start: (now - start) / 60000,
    duration: Math.min(1440, (gameStamp(moment.time) - now) / 60000),
    lane: moment.key.startsWith('party-') ? 'outer' : 'inner',
    tone: moment.key.startsWith('party-') ? 'expedition' : 'task', label: moment.label,
  }))
}

export function clockSegments(data: DayTimeline | null, now: number) {
  if (!data) return []
  const minutes = (stamp: number) => (stamp - data.day_start) / 60
  const segments = data.expeditions.map(e => ({
    key: e.key, start: e.time_min, duration: e.duration_min, lane: 'outer',
    tone: e.kind === 'running' || e.will_run ? 'expedition' : 'inactive',
    label: `${numerals[e.team_no] || e.team_no}队远征 ${e.map_code}`,
  }))
  for (const [index, run] of data.runs.entries()) segments.push({
    key: `run-${index}`, start: minutes(run.started_at),
    duration: Math.max(0, minutes(run.ended_at ?? Math.min(now / 1000, data.day_start + 86400)) - minutes(run.started_at)),
    lane: 'inner', tone: run.status === 'running' ? 'running' : run.tone === 'failed' ? 'failed' : 'task', label: run.label,
  })
  for (const [index, block] of (data.booking?.blocks || []).entries()) {
    const state = data.conductor.blocks.find(b => b.start_min === block.start_min && b.kind === block.kind && b.script === block.script)
    if (state && state.status !== 'pending') continue
    const activity = block.kind === 'raid' || block.kind === 'activity' ? data.activity : null
    // No estimated length for tasks without a measured duration: show a point on the dial.
    const duration = activity?.seconds_per_loop ? (block.runs || 1) * activity.seconds_per_loop / 60 : 0
    segments.push({ key: `booking-${index}`, start: block.start_min, duration, lane: 'inner', tone: 'planned',
      label: state?.label || data.gameplay_options?.find(o => o.script === block.script)?.label || (block.kind === 'daily' ? '一键日课' : block.kind === 'raid' ? '联队战' : '任务流') })
  }
  return segments.filter(s => s.start < 1680 && s.start + s.duration >= 0)
}
