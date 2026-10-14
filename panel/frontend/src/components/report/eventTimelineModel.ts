const SHANGHAI_OFFSET_MS = 8 * 3600 * 1000

/** 国服以北京时间凌晨 4 点换日；返回当前时刻之后的下一次换日。 */
export function nextCnDailyReset(nowMs: number): number {
  const shanghai = new Date(nowMs + SHANGHAI_OFFSET_MS)
  const year = shanghai.getUTCFullYear()
  const month = shanghai.getUTCMonth()
  const day = shanghai.getUTCDate()
  const hour = shanghai.getUTCHours()
  const resetDay = day + (hour >= 4 ? 1 : 0)
  return Date.UTC(year, month, resetDay, 4) - SHANGHAI_OFFSET_MS
}

/**
 * now 到 end 之间还剩几个「完整游戏日」（凌晨 4 点换日口径）。
 * 当前游戏日算 1 个；收摊前不足一天的零头（比如凌晨 5 点收摊只剩
 * 04:00–05:00）并入最后一天，不单独占一天——不把最后一圈摊到凌晨。
 * 与后端 touken/advisor.py 的 game_days_remaining 同一口径。
 */
export function gameDaysRemaining(nowMs: number, endMs: number): number {
  if (!Number.isFinite(nowMs) || !Number.isFinite(endMs) || endMs <= nowMs) return 1
  const reset = nextCnDailyReset(nowMs)
  if (endMs <= reset) return 1
  return 1 + Math.floor((endMs - reset) / 86400000)
}

/**
 * 把活动剩余圈数摊到剩余完整游戏日里。
 * 向上取整，避免每天抹掉小数后在最后差几圈；最后一天（含收摊零头）报全部剩余圈数。
 */
export function dailyRunTarget(runsNeeded: number | null | undefined, secondsToEnd: number | null | undefined, nowMs: number): number | null {
  if (!Number.isFinite(runsNeeded) || Number(runsNeeded) <= 0) return null
  if (!Number.isFinite(secondsToEnd) || Number(secondsToEnd) <= 0) return null
  if (!Number.isFinite(nowMs)) return null
  const runs = Math.ceil(Number(runsNeeded))
  const daysLeft = gameDaysRemaining(Number(nowMs), Number(nowMs) + Number(secondsToEnd) * 1000)
  return Math.min(runs, Math.max(1, Math.ceil(runs / daysLeft)))
}

export interface ImmediateBatchPlan {
  runs: number
  finishAtMs: number
  horizonAtMs: number
  horizon: 'daily-reset' | 'event-end'
  reserveSeconds: number
}

/**
 * 给“现在这一锅”算一个能直接填进任务的保守圈数。
 * 不超过今日平均目标、活动剩余圈数和任务表单上限，并给 4 点换日或收摊留出收尾时间。
 */
export function immediateBatchPlan(
  runsNeeded: number | null | undefined,
  secondsPerLoop: number | null | undefined,
  secondsToEnd: number | null | undefined,
  nowMs: number,
  reserveSeconds = 300,
  maxRuns = 99,
): ImmediateBatchPlan | null {
  const dailyRuns = dailyRunTarget(runsNeeded, secondsToEnd, nowMs)
  if (dailyRuns == null || !Number.isFinite(secondsPerLoop) || Number(secondsPerLoop) <= 0) return null
  if (!Number.isFinite(nowMs) || !Number.isFinite(secondsToEnd) || Number(secondsToEnd) <= 0) return null

  const resetAtMs = nextCnDailyReset(nowMs)
  const eventEndAtMs = nowMs + Number(secondsToEnd) * 1000
  const horizon = eventEndAtMs < resetAtMs ? 'event-end' : 'daily-reset'
  const horizonAtMs = Math.min(resetAtMs, eventEndAtMs)
  const usableSeconds = Math.max(0, Math.floor((horizonAtMs - nowMs) / 1000) - reserveSeconds)
  const capacity = Math.max(0, Math.floor(usableSeconds / Number(secondsPerLoop)))
  const runs = Math.min(Math.ceil(Number(runsNeeded)), dailyRuns, maxRuns, capacity)
  return {
    runs,
    finishAtMs: nowMs + runs * Number(secondsPerLoop) * 1000,
    horizonAtMs,
    horizon,
    reserveSeconds,
  }
}
