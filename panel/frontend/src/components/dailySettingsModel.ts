import type { WorkflowNode, WorkflowNodeDef, WorkflowPreset } from '../types'
function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T }
export type DailyRow = { id: string; label: string; enabled: boolean; nodes: WorkflowNode[] }
export const dailyGroups = [
  ['boot', '启动游戏', 'boot_emulator', 'login'],
  ['signin', '签到领鸡蛋', 'signin', 'free_gift'],
  ['practice', '演练', 'practice'], ['naihanka', '内番', 'naihanka'],
  ['forge', '锻刀刀解', 'forge', 'dismantle'], ['expedition', '远征', 'expedition'],
  ['yosari', '异去', 'yosari'], ['activity', '当期活动'],
  ['rewards', '任务奖励', 'task_rewards'], ['inbox', '领杂物箱', 'inbox_supplies'],
  ['sugar', '炼糖', 'sugar'],
]
export function makeNode(type: string, defs: WorkflowNodeDef[]): WorkflowNode {
  const def = defs.find(d => d.type === type)
  return {type, on_error: 'stop', params: Object.fromEntries((def?.params || []).filter(f => f.type !== 'note').map(f => [f.key, clone(f.default ?? '')]))}
}
export function dailyRows(preset: WorkflowPreset, defs: WorkflowNodeDef[], activity: string): DailyRow[] {
  if (preset.daily_ui?.rows) return clone(preset.daily_ui.rows)
  const used = new Set<WorkflowNode>()
  const rows = dailyGroups.map(([id, label, ...types]) => {
    if (id === 'activity' && activity) types = [activity]
    const found = preset.nodes.filter(n => types.includes(n.type))
    found.forEach(n => used.add(n))
    return {id: id!, label: label!, enabled: !!found.length || id === 'boot', nodes: types.flatMap(t => { const matches = found.filter(n => n.type === t); return matches.length ? clone(matches) : [makeNode(t, defs)] })}
  })
  const oldSortie = preset.nodes.find(n => n.type === 'daily_sortie')
  if (oldSortie) {
    used.add(oldSortie)
    const mode = String(oldSortie.params.sortie_mode || 'none')
    const row = rows.find(r => r.id === (mode === 'yosari' ? 'yosari' : 'activity'))!
    if (mode !== 'none') {
      const node = makeNode(mode, defs)
      node.params = {...node.params, ...clone(oldSortie.params)}
      if (mode === 'yosari') Object.assign(node.params, {map_no: oldSortie.params.yosari_map_no ?? 1, loops: oldSortie.params.yosari_runs ?? 1, auto_refill: oldSortie.params.yosari_auto_refill ?? false})
      if (mode === 'raid') Object.assign(node.params, {runs: oldSortie.params.raid_rounds ?? 3, auto_refill: true})
      if (mode === 'osaka') Object.assign(node.params, {floors: oldSortie.params.osaka_runs ?? 1, select_floor: oldSortie.params.osaka_select_floor ?? false, target_floor: oldSortie.params.osaka_target_floor ?? 81})
      if (mode === 'pumpkin') Object.assign(node.params, {difficulty: oldSortie.params.pumpkin_difficulty ?? 1, runs: oldSortie.params.pumpkin_runs ?? 4, watch: oldSortie.params.pumpkin_watch ?? ''})
      row.nodes = [node]; row.enabled = true
    }
  }
  const extras = preset.nodes.filter(n => !used.has(n) && !['snapshot', 'ledger_sync'].includes(n.type))
  if (extras.length) rows.push({id:'custom',label:'原有其他步骤',enabled:true,nodes:clone(extras)})
  // 原来没有启动步骤的默认日课补上模拟器启动，登录仍由共用安全流程完成。
  const boot = rows.find(r => r.id === 'boot')!
  if (!boot.nodes.some(n => n.type === 'boot_emulator')) boot.nodes.unshift(makeNode('boot_emulator', defs))
  return rows
}
export function compileDaily(rows: DailyRow[], tail: WorkflowNode[]): WorkflowNode[] {
  return clone([...rows.filter(r => r.enabled).flatMap(r => r.nodes), ...tail])
}
