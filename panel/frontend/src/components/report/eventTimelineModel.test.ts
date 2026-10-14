import { describe, expect, it } from 'vitest'
import { dailyRunTarget, gameDaysRemaining, immediateBatchPlan, nextCnDailyReset } from './eventTimelineModel'

const NOON = Date.parse('2026-09-20T12:00:00+08:00')

describe('活动每日挂机圈数', () => {
  it('按剩余完整游戏日向上取整，给出可以直接开工的圈数', () => {
    // 9-20 12:00 + 104h52m → 9-24 20:52：换日后还有 3 个完整日，共摊 4 天
    expect(dailyRunTarget(338, 104 * 3600 + 52 * 60, NOON)).toBe(85)
  })

  it('明早 5 点收摊时今天就是最后一天，剩余圈数今天收齐', () => {
    // 12:00 → 次日 05:00：收摊零头 04:00–05:00 不单独占一天
    expect(dailyRunTarget(27, 17 * 3600, NOON)).toBe(27)
  })

  it('不足一天时不会报出比全部剩余圈数更多的任务', () => {
    expect(dailyRunTarget(12, 6 * 3600, NOON)).toBe(12)
  })

  it('没有有效圈数或截止时间时保持未知', () => {
    expect(dailyRunTarget(null, 86400, NOON)).toBeNull()
    expect(dailyRunTarget(10, 0, NOON)).toBeNull()
  })
})

describe('剩余完整游戏日', () => {
  it('收摊在下次换日前只剩今天', () => {
    expect(gameDaysRemaining(NOON, Date.parse('2026-09-20T20:00:00+08:00'))).toBe(1)
    expect(gameDaysRemaining(NOON, Date.parse('2026-09-21T04:00:00+08:00'))).toBe(1)
  })

  it('换日后的零头并入最后一天，完整日才另算', () => {
    // 明早 5 点收摊：只剩 04:00–05:00 一小时零头，不算一天
    expect(gameDaysRemaining(NOON, Date.parse('2026-09-21T05:00:00+08:00'))).toBe(1)
    // 后天凌晨 4 点收摊：今天 + 明天一个完整日
    expect(gameDaysRemaining(NOON, Date.parse('2026-09-22T04:00:00+08:00'))).toBe(2)
    expect(gameDaysRemaining(NOON, Date.parse('2026-09-22T05:00:00+08:00'))).toBe(2)
  })
})

describe('活动现在这一锅', () => {
  it('北京时间凌晨四点换日', () => {
    expect(nextCnDailyReset(Date.parse('2026-09-20T02:34:00+08:00')))
      .toBe(Date.parse('2026-09-20T04:00:00+08:00'))
    expect(nextCnDailyReset(Date.parse('2026-09-20T12:00:00+08:00')))
      .toBe(Date.parse('2026-09-21T04:00:00+08:00'))
  })

  it('临近换日前按实测圈速留五分钟收尾，给出可直接开工的圈数', () => {
    const now = Date.parse('2026-09-20T02:34:00+08:00')
    const plan = immediateBatchPlan(338, 5 * 60 + 40, 98 * 3600, now)
    expect(plan).toMatchObject({ runs: 14, horizon: 'daily-reset', reserveSeconds: 300 })
    expect(plan?.finishAtMs).toBe(Date.parse('2026-09-20T03:53:20+08:00'))
  })

  it('白天有充足时间时只安排当天平均目标，不把全部欠账塞进一锅', () => {
    const now = Date.parse('2026-09-20T12:00:00+08:00')
    expect(immediateBatchPlan(338, 5 * 60 + 40, 98 * 3600, now)?.runs).toBe(85)
  })

  it('收摊早于换日时以收摊为界，来不及一圈就劝先别开', () => {
    const now = Date.parse('2026-09-20T03:50:00+08:00')
    const plan = immediateBatchPlan(20, 6 * 60, 9 * 60, now)
    expect(plan).toMatchObject({ runs: 0, horizon: 'event-end' })
  })
})
