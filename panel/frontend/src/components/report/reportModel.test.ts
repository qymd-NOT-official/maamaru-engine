import { describe, expect, it } from 'vitest'
import type { LedgerAttribution } from '../../types'
import { activityTitle, activityStep, categoryOf, gameLedgerRecords, recordOrigin, honmaruReceipts, shiftShanghaiDate, swordReceiptEntries } from './reportModel'

it('日报翻页按上海时区日历日走，差一秒不串天', () => {
  expect(shiftShanghaiDate('2026-10-06', -1)).toBe('2026-10-05')
  expect(shiftShanghaiDate('2026-10-04', 1)).toBe('2026-10-05')
  expect(shiftShanghaiDate('2026-10-01', -1)).toBe('2026-09-30')
  expect(shiftShanghaiDate('2026-12-31', 1)).toBe('2027-01-01')
})

it('运行状态翻译内部名称并保留玩家命名', () => {
  expect(activityTitle({ label: 'workflow' })).toBe('自定义工作流')
  expect(activityTitle({ label: '活动+异去' })).toBe('活动+异去')
  expect(activityStep({ step: 'raid:hailian' })).toBe('联队战')
  expect(activityStep({ step: 'future_internal_step' })).toBe('任务进行中')
  expect(activityStep({ step: '正在补充刀装' })).toBe('正在补充刀装')
})

it('收件箱记录按振展开，同一已知掉落实例不重复计数', () => {
  const events = [
    { event_type: 'sword.obtained', payload: { name: '物吉贞宗', serial_id: 1 } },
    { event_type: 'sword.inbox_received', payload: { swords: [
      { name: '物吉贞宗', serial_id: 1 }, { name: '火车切', serial_id: 2 }] } },
  ]
  expect(swordReceiptEntries(events).map(e => e.payload.name)).toEqual(['物吉贞宗', '火车切'])
})

it('counts every sword in a batch including duplicate names, alongside battle drops', () => {
  const batch = { event_type: 'forge.collected', payload: { swords: Array.from({ length: 10 }, (_, i) => ({ name: '堀川国广', serial_id: i + 1 })) } }
  const entries = swordReceiptEntries([batch, { event_type: 'sword.obtained', payload: { name: '宗三左文字' } }, { event_type: 'forge.started', payload: { name: '不应计入' } }])
  expect(entries).toHaveLength(11)
  expect(entries.filter(event => event.payload.name === '堀川国广')).toHaveLength(10)
})

const receipt = (resource: string, delta: number, eventId: number): LedgerAttribution => ({
  id: `a${eventId}`, event_id: eventId, ts: 1790733600, resource, delta,
  source: 'expedition.youzu_log.conquest/complete', script: 'youzu_log',
  label: `远征完成·一队·A1 鸟羽之战 ${resource} ${delta > 0 ? '+' : ''}${delta}`,
})

describe('game receipts and caretaker execution', () => {
  it('puts translated game sources into the matching ledger category', () => {
    expect(categoryOf(receipt('木炭', 15, 1).source)).toBe('expedition')
    expect(categoryOf('artifact.youzu_log.artifact/buybindingagent')).toBe('artifact')
  })
  it('groups resources from one receipt and keeps map detail without duplicate events', () => {
    const a = receipt('木炭', 15, 1), b = receipt('玉钢', 20, 2)
    const groups = gameLedgerRecords([a, b, a])
    expect(groups).toHaveLength(1)
    expect(groups[0].payload.label).toBe('远征完成·一队·A1 鸟羽之战')
    expect(groups[0].payload.resources).toEqual({ 木炭: 15, 玉钢: 20 })
  })
  it('labels game evidence separately from a linked execution', () => {
    const a = receipt('木炭', 15, 1)
    expect(recordOrigin(a)).toBe('游戏记录')
    expect(recordOrigin({ ...a, run_label: '晚间收菜', execution_script: 'workflow' })).toBe('游戏记录 · まあ丸执行晚间收菜')
    expect(recordOrigin({ ...a, script: 'workflow', run_id: 'named' }, [{ run_id: 'named', label: '一键日课' }])).toBe('まあ丸 · 一键日课')
    expect(recordOrigin({ ...a, run_id: 'run-a', execution_script: 'expedition' })).toBe('游戏记录 · まあ丸执行远征')
    expect(gameLedgerRecords([{ ...a, script: 'expedition' }])).toEqual([])
  })
})

describe('honmaru receipt summary', () => {
  it('keeps spending visible even when the same activity earns more', () => {
    const items = [receipt('小判', 1000, 1), receipt('小判', -300, 2)]
    expect(honmaruReceipts(items, 'gain')[0].total).toBe(1000)
    expect(honmaruReceipts(items, 'cost')[0].total).toBe(300)
  })
  it('uses only known resources and sources and keeps resource ordering', () => {
    const items = [receipt('木炭', 5000, 1), receipt('小判', 20, 2),
      { ...receipt('小判', 10000, 3), source: 'unknown.youzu_log' }, receipt('审神者经验', 20, 4)]
    expect(honmaruReceipts(items, 'gain').map(i => [i.resource, i.total])).toEqual([['小判', 20], ['木炭', 5000]])
    expect(honmaruReceipts([], 'cost')).toEqual([])
  })
})


it('links receipts only with matching operation evidence and preserves ambiguous owners', async () => {
  const { linkReceiptRuns } = await import('./reportModel')
  const receipt = { id: 'a', ts: 100, resource: '木炭', delta: 1550, source: 'task_rewards.youzu_log', script: 'youzu_log' }
  const event = { run_id: 'daily', ts: 102, script: 'workflow', event_type: 'resource.change', payload: { resource: '木炭', delta: 1550, source: 'task_rewards.reward_popup' } }
  expect(linkReceiptRuns([receipt], [], [{ run_id: 'daily', started_at: 90, ended_at: 120 }])[0].run_id).toBeUndefined()
  expect(linkReceiptRuns([receipt], [event], [{ run_id: 'daily', label: '一键日课' }])[0].run_id).toBe('daily')
  expect(linkReceiptRuns([receipt], [event, { ...event, run_id: 'other' }], [])[0].run_id).toBeUndefined()
})


it('links raid rewards using recorded round rewards rather than task time alone', async () => {
  const { linkReceiptRuns } = await import('./reportModel')
  const receipt = { id: 'raid', ts: 100, resource: '活动点数·10031', delta: 756, source: 'raid.youzu_log', script: 'youzu_log' }
  const event = { ts: 120, run_id: 'raid-task', event_type: 'raid.round_completed', payload: { shells: 756, sequence: 1 } }
  expect(linkReceiptRuns([receipt], [event], [])[0].run_id).toBe('raid-task')
  expect(linkReceiptRuns([receipt], [{ ...event, payload: { shells: 700 } }], [])[0].run_id).toBeUndefined()
})

it('links client ticket costs only to explicit refills with an unambiguous owner', async () => {
  const { linkReceiptRuns } = await import('./reportModel')
  const receipt = { id: 'ticket', ts: 100, resource: '小判', delta: -300, source: 'ticket.youzu_log.sally/recovercost', script: 'youzu_log' }
  const event = { ts: 104, run_id: 'raid-task', script: 'workflow', event_type: 'ticket.refilled', payload: { source: 'RAID' } }
  expect(linkReceiptRuns([receipt], [event], [])[0].run_id).toBe('raid-task')
  expect(linkReceiptRuns([receipt], [event], [])[0].label).toBe('联队战补充手形')
  expect(linkReceiptRuns([receipt], [], [{ run_id: 'raid-task', started_at: 90, ended_at: 120 }])[0].run_id).toBeUndefined()
  expect(linkReceiptRuns([receipt], [{ ...event, payload: { source: 'RAID', ticket_price: 600 } }], [])[0].run_id).toBeUndefined()
  expect(linkReceiptRuns([receipt], [event, { ...event, run_id: 'other' }], [])[0].run_id).toBeUndefined()
  expect(linkReceiptRuns([{ ...receipt, source: 'shop.youzu_log.shop/buy' }], [event], [])[0].run_id).toBeUndefined()
})
