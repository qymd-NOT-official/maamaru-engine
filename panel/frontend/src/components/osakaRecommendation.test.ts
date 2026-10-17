import { describe, expect, it } from 'vitest'
import { osakaRecommendation } from './osakaRecommendation'

describe('大阪城三点前推荐圈数', () => {
  const now = new Date(2026, 9, 17, 23)
  const sample = { started_at: now.getTime() / 1000 - 3600, loops: 10, average_loop_seconds: 300, selected_floor: 99 }
  it('按同层圈速扣除日课和波动余量，向下取整', () => {
    expect(osakaRecommendation([sample], 99, '', now).runs).toBe(36)
    expect(osakaRecommendation([sample], 99, '01:00', now).runs).toBe(15)
  })
  it('凌晨不多算一天，三点至换日前不推荐', () => {
    expect(osakaRecommendation([sample], 99, '', new Date(2026, 9, 18, 1)).runs).toBe(15)
    expect(osakaRecommendation([sample], 99, '', new Date(2026, 9, 18, 3, 10)).runs).toBe(0)
  })
  it('排除过期、错层、空速及未来记录', () => {
    expect(osakaRecommendation([sample], 88, '', now).runs).toBe(0)
    for (const patch of [{ started_at: sample.started_at - 15 * 86400 }, { average_loop_seconds: NaN }, { loops: 0 }, { started_at: now.getTime() / 1000 + 1 }]) {
      expect(osakaRecommendation([{ ...sample, ...patch }], 99, '', now).runs).toBe(0)
    }
  })
  it('遵守次数上限，太迟开始不填零圈', () => {
    expect(osakaRecommendation([{ ...sample, average_loop_seconds: 10 }], 99, '', now).runs).toBe(99)
    expect(osakaRecommendation([sample], 99, '03:30', now).runs).toBe(0)
  })
})
