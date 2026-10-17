export interface OsakaPaceRun {
  started_at: number
  loops: number
  average_loop_seconds: number | null
  selected_floor: number | null
}

// 日课以凌晨四点换日；三点后不再推荐本日的长时间出阵。
export function osakaRecommendation(runs: OsakaPaceRun[], floor: number | null, startTime = '', now = new Date()) {
  const deadline = new Date(now)
  deadline.setHours(3, 0, 0, 0)
  if (now.getHours() >= 4) deadline.setDate(deadline.getDate() + 1)
  const start = new Date(now)
  if (startTime) {
    const [hour, minute] = startTime.split(':').map(Number)
    start.setHours(hour!, minute!, 0, 0)
    if (hour! < 4 && now.getHours() >= 4) start.setDate(start.getDate() + 1)
    if (start < now) start.setTime(now.getTime())
  }
  const sample = [...runs].sort((a, b) => b.started_at - a.started_at).find(run =>
    Number.isFinite(run.average_loop_seconds) && Number(run.average_loop_seconds) > 0
    && run.loops > 0 && run.started_at >= now.getTime() / 1000 - 14 * 86400
    && run.started_at <= now.getTime() / 1000 && run.selected_floor === floor)
  if (!sample) return { runs: 0, reason: '近 14 天没有相同层数的有效圈速记录，先手动跑几圈再来看看。' }
  // 给前置日课留半小时，实测圈速再加 15% 余量。
  const count = Math.max(0, Math.floor(((deadline.getTime() - start.getTime()) / 1000 - 1800) / (Number(sample.average_loop_seconds) * 1.15)))
  return { runs: Math.min(99, count), reason: count > 0
    ? `按近期${floor == null ? '未指定层数' : `${floor} 层`}实测约 ${Math.round(Number(sample.average_loop_seconds))} 秒/圈估算，预留前置日课 30 分钟及 15% 余量；最多填入 99 圈。耗时变化或手入可能延后，请留意。`
    : '距离本日凌晨 3 点已不足，暂不推荐出阵；可手动填写圈数。' }
}
