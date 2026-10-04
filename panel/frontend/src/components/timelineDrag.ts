export function draggedMinute(start: number, deltaPx: number, axisWidth: number, now: number): number {
  const minimum = Math.ceil(now / 15) * 15
  return Math.max(minimum, Math.min(1665, Math.round((start + deltaPx / axisWidth * 1680) / 15) * 15))
}
