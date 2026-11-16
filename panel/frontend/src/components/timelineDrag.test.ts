import { describe, expect, it } from 'vitest'
import { draggedMinute, paintedMinute } from './timelineDrag'

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

describe('空白处画块', () => {
  it('按泳道内偏移换算并吸附15分钟', () => {
    expect(paintedMinute(400, 1120, 500)).toBe(600)
    expect(paintedMinute(401, 1120, 500)).toBe(600)
    expect(paintedMinute(410, 1120, 500)).toBe(615)
  })
  it('不画到过去或次日换日之后', () => {
    expect(paintedMinute(100, 1120, 500)).toBe(510)
    expect(paintedMinute(-50, 1120, 601)).toBe(615)
    expect(paintedMinute(2000, 1120, 500)).toBe(1665)
  })
  it('与拖动已有块共用同一套钳制', () => {
    expect(paintedMinute(300, 1120, 480)).toBe(draggedMinute(0, 300, 1120, 480))
  })
})
