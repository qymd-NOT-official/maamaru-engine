import { describe, expect, it } from 'vitest'
import { draggedMinute } from './timelineDrag'

describe('时间表拖动', () => {
  it('按全天轴宽换算并吸附15分钟', () => {
    expect(draggedMinute(600, 20, 1120, 500)).toBe(630)
    expect(draggedMinute(600, -20, 1120, 500)).toBe(570)
  })
  it('不拖到过去或次日换日之后', () => {
    expect(draggedMinute(600, -1000, 1120, 601)).toBe(615)
    expect(draggedMinute(1600, 1000, 1120, 500)).toBe(1665)
  })
})
