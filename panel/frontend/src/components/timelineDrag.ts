export function draggedMinute(start: number, deltaPx: number, axisWidth: number, now: number): number {
  const minimum = Math.ceil(now / 15) * 15
  return Math.max(minimum, Math.min(1665, Math.round((start + deltaPx / axisWidth * 1680) / 15) * 15))
}

/** 画块：把泳道内的横向偏移（像素）换算成吸附后的分钟数，钳制规则与拖动已有块一致 */
export function paintedMinute(offsetPx: number, axisWidth: number, now: number): number {
  return draggedMinute(0, offsetPx, axisWidth, now)
}
