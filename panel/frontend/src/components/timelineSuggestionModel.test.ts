import { describe, expect, it } from 'vitest'
import { suggestionBooked, suggestionDraftTaken, suggestionTitle, suggestionToBlock } from './timelineSuggestionModel'
import type { DayScheduleBlock, DayTimelineSuggestion } from '../types'

const raidSuggestion: DayTimelineSuggestion = { start_min: 500, duration_min: 60, runs: 6, note: '' }
const hangSuggestion: DayTimelineSuggestion = { start_min: 500, duration_min: 60, note: '' }
const edocastleSuggestion: DayTimelineSuggestion = {
  start_min: 600, duration_min: 30, runs: 6, note: '避开08:00 部队三派遣',
  script: 'edocastle', label: '江户城潜入调查', event_key: 'evt-1', end_at: 1_800_000_000, basis: 'saved',
}
const scheduleMinute = (value: string) => {
  if (!/^\d{2}:\d{2}$/.test(value)) return null
  const [hour, minute] = value.split(':').map(Number)
  return hour < 24 && minute < 60 ? hour * 60 + minute + (hour < 4 ? 1440 : 0) : null
}

describe('时间表建议淡影', () => {
  it('按建议类型出显示名，不假装有目标规划', () => {
    expect(suggestionTitle(raidSuggestion)).toBe('联队战 6 圈')
    expect(suggestionTitle(hangSuggestion)).toBe('挂机建议')
    expect(suggestionTitle(edocastleSuggestion)).toBe('江户城潜入调查 6 次')
  })

  it('已排同玩法的正式安排后对应淡影让位，其他淡影不受影响', () => {
    const bookedRaid: DayScheduleBlock = { start_min: 500, kind: 'raid', runs: 6 }
    expect(suggestionBooked(raidSuggestion, [bookedRaid])).toBe(true)
    expect(suggestionBooked(edocastleSuggestion, [bookedRaid])).toBe(false)
    // 挂机建议没有玩法身份，永远显示
    expect(suggestionBooked(hangSuggestion, [bookedRaid])).toBe(false)
    const bookedEdocastle: DayScheduleBlock = { start_min: 600, kind: 'activity', script: 'edocastle', event_key: 'evt-1', runs: 6 }
    expect(suggestionBooked(edocastleSuggestion, [bookedEdocastle])).toBe(true)
    const bookedOther: DayScheduleBlock = { start_min: 600, kind: 'activity', script: 'pumpkin', event_key: 'evt-2', runs: 4 }
    expect(suggestionBooked(edocastleSuggestion, [bookedOther])).toBe(false)
  })

  it('淡影转安排块：活动块带期钥匙，圈数块保持联队战口径', () => {
    expect(suggestionToBlock(edocastleSuggestion)).toEqual({
      start_min: 600, kind: 'activity', script: 'edocastle', event_key: 'evt-1', runs: 6,
    })
    expect(suggestionToBlock(raidSuggestion)).toEqual({ start_min: 500, kind: 'raid', runs: 6 })
  })

  it('草稿采纳过同玩法同时刻的建议后不再画淡影', () => {
    const draft = [
      { kind: 'activity', script: 'edocastle', time: '10:00' },
      { kind: 'raid', script: '', time: '08:20' },
      { kind: 'workflow', workflow_id: 'wf', time: '09:00' },
    ]
    expect(suggestionDraftTaken(edocastleSuggestion, draft, scheduleMinute)).toBe(true)
    expect(suggestionDraftTaken({ ...edocastleSuggestion, start_min: 605 }, draft, scheduleMinute)).toBe(false)
    expect(suggestionDraftTaken(raidSuggestion, draft, scheduleMinute)).toBe(true)
    expect(suggestionDraftTaken({ ...raidSuggestion, start_min: 540 }, draft, scheduleMinute)).toBe(false)
    expect(suggestionDraftTaken({ ...raidSuggestion, start_min: 1560 }, draft, scheduleMinute)).toBe(false)
    expect(suggestionDraftTaken(hangSuggestion, draft, scheduleMinute)).toBe(false)
  })
})
