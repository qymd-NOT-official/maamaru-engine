import { describe, expect, it } from 'vitest'
import type { DayTimeline, HonmaruSituation } from '../types'
import { clockSegments, homeMoments, remainingTime, situationSegments } from './homeClockModel'

const now = Date.parse('2026-10-03T23:00:00+08:00')
describe('home countdowns and clock', () => {
  it('draws JP observed remaining time without importing a task plan or completed timers', () => {
    const state = { parties: [{ party_no: 2, finished_at: '2026-10-04 01:00:00' }],
      kiwame_return: [], forge_slots: [{ slot_no: 1, finished_at: '2026-10-03 22:00:00' }],
      duty: { finished_at: '2026-10-04 02:00:00' } } as unknown as HonmaruSituation
    expect(situationSegments(state, now)).toEqual([
      { key: 'party-2', start: 1380, duration: 120, lane: 'outer', tone: 'expedition', label: '二队远征归来' },
      { key: 'duty', start: 1380, duration: 180, lane: 'inner', tone: 'task', label: '内番完成' },
    ])
    expect(situationSegments(null, now)).toEqual([])
  })
  it('keeps every valid timer with readable names and does not claim collection', () => {
    const state = { parties: [{ party_no: 5, finished_at: '2026-10-04 01:00:00' }],
      kiwame_return: [], forge_slots: [{ slot_no: 3, finished_at: '2026-10-03 22:00:00' }],
      repair: [{ slot_no: 1, finished_at: 'invalid' }], duty: { finished_at: '2026-10-04 02:00:00' } } as unknown as HonmaruSituation
    const moments = homeMoments(state, now)
    expect(moments.map(m => m.label)).toEqual(['三号炉完成', '五队远征归来', '内番完成'])
    expect(remainingTime(moments[0]!.time, now)).toBe('预计已完成')
    expect(remainingTime(moments[1]!.time, now)).toBe('2:00:00')
    expect(remainingTime('2026-10-03 23:00:01', now)).toBe('00:01')
    expect(homeMoments(null, now)).toEqual([])
  })
  it('uses actual run duration across midnight and leaves unknown task duration as a point', () => {
    const data = { day_start: now / 1000 - 23 * 3600, expeditions: [],
      runs: [{ started_at: now / 1000, ended_at: now / 1000 + 7200, label: '跨日出阵', tone: 'ok', status: 'completed' }],
      booking: { blocks: [{ start_min: 1410, kind: 'daily' }] }, conductor: { blocks: [] }, activity: null } as unknown as DayTimeline
    const segments = clockSegments(data, now)
    expect(segments[0]).toMatchObject({ start: 1380, duration: 120 })
    expect(segments[1]).toMatchObject({ start: 1410, duration: 0, label: '一键日课' })
    expect(clockSegments(null, now)).toEqual([])
  })
})
