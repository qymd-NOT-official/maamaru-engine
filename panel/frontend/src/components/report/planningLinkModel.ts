import type { DayTimeline } from '../../types'

/** New work starts no earlier than now or the previous block's estimated end. */
export function nextScheduledStart(nowMin: number, previousEndMin: number): number {
  return Math.min(1679, Math.max(Math.ceil(nowMin), previousEndMin))
}

export interface RaidDayRecommendation {
  available: boolean
  runs: number
  targetRuns: number
  reason: string
}

/** Use only the day's schedule snapshot; never derive a second daily loop estimate here. */
export function raidDayRecommendation(day: DayTimeline | null | undefined, isRaidEvent: boolean): RaidDayRecommendation | null {
  if (!isRaidEvent) return null
  const activity = day?.activity
  if (!activity || activity.name !== '联队战') {
    return { available: false, runs: 0, targetRuns: 0, reason: day?.hint || '今日时间表数据暂不可用，请稍后刷新。' }
  }
  if (activity.target_runs <= 0) {
    return { available: false, runs: 0, targetRuns: 0, reason: '联队战今天的进度已够，等下一次记账再更新。' }
  }
  const suggestions = (day?.suggestions || []).filter(item => !item.script && Number.isInteger(item.runs) && Number(item.runs) > 0)
  const runs = suggestions.reduce((total, item) => total + Number(item.runs), 0)
  if (!runs) return {
    available: false,
    runs: 0,
    targetRuns: activity.target_runs,
    reason: activity.seconds_per_loop <= 0
      ? '本期实测圈速还不够，暂时排不出挂机时段。'
      : '今天的空档排不出联队战时段。',
  }
  return { available: true, runs, targetRuns: activity.target_runs, reason: '' }
}

export function canAdoptRaidRecommendation(hasRecommendation: boolean, editing: boolean, hasSavedBlocks: boolean): boolean {
  return hasRecommendation && !editing && !hasSavedBlocks
}

/** Clock inputs before 04:00 belong to the following morning. */
export function scheduleMinute(value: string): number | null {
  if (!/^\d{2}:\d{2}$/.test(value)) return null
  const [hour, minute] = value.split(':').map(Number)
  return hour < 24 && minute < 60 ? hour * 60 + minute + (hour < 4 ? 1440 : 0) : null
}
