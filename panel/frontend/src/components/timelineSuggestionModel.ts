import type { DayScheduleBlock, DayTimelineSuggestion } from '../types'

/** 建议块显示名：非代币活动带玩法名，圈数建议=联队战，无圈数=挂机建议。 */
export function suggestionTitle(s: DayTimelineSuggestion): string {
  if (s.script) return `${s.label || s.script} ${s.runs ?? '?'} 次`
  if (s.runs != null) return `联队战 ${s.runs} 圈`
  return '挂机建议'
}

/** 已排了同玩法的正式安排，对应淡影就该让位（挂机建议没有玩法身份，永远显示）。 */
export function suggestionBooked(s: DayTimelineSuggestion, blocks: Array<Pick<DayScheduleBlock, 'kind' | 'script'>>): boolean {
  if (s.script) return blocks.some(b => b.kind === 'activity' && b.script === s.script)
  if (s.runs != null) return blocks.some(b => b.kind === 'raid' || (b.kind === 'activity' && b.script === 'raid'))
  return false
}

/** 淡影 → 今日安排的草稿块；activity 块带上期钥匙，缺省按联队战圈数块处理。 */
export function suggestionToBlock(s: DayTimelineSuggestion): DayScheduleBlock {
  if (s.script) {
    return { start_min: s.start_min, kind: 'activity', script: s.script, event_key: s.event_key || 'permanent', runs: s.runs ?? 1 }
  }
  return { start_min: s.start_min, kind: 'raid', runs: s.runs ?? 1 }
}

/** 草稿里已采纳过这段建议（同玩法同开工时间）就不再画淡影。 */
export function suggestionDraftTaken(s: DayTimelineSuggestion, draftRows: Array<{ kind: string; script?: string; time: string }>, parseTime: (value: string) => number | null): boolean {
  if (s.runs == null && !s.script) return false
  return draftRows.some(row => {
    if (row.kind !== 'raid' && row.kind !== 'activity') return false
    if (s.script ? row.script !== s.script : row.kind !== 'raid') return false
    return parseTime(row.time) === s.start_min
  })
}
