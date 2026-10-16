import { describe, expect, it } from 'vitest'
import { buildJournalPosts, runPostText } from './honmaruJournal'

describe('Osaka return journal', () => {
  const session = { source: 'game_rewards', delta: 5250, floors: 5 }
  it('reports this run income and floors, not current balance or period income', () => {
    const text = runPostText({ status: 'completed', koban_session: session, loops: 99, resource_delta: { 小判: 900000 } })
    expect(text).toContain('捞到了 5,250 小判')
    expect(text).toContain('挖了 5 层')
    expect(text).not.toContain('900,000')
  })
  it.each(['failed', 'stopped'])('keeps %s runs honest while reporting confirmed rewards', status => {
    const text = runPostText({ status, koban_session: session })
    expect(text).toContain('已确认捞到 5,250 小判')
    expect(text).not.toContain('回来啦')
  })
  it('does not invent rewards from old balance differences or missing results', () => {
    for (const koban_session of [undefined, { before: 100, after: 1000 }, { source: 'game_rewards', delta: null }, { source: 'game_rewards', delta: -1 }]) {
      expect(runPostText({ status: 'completed', koban_session })).not.toContain('捞到')
    }
    expect(runPostText({ status: 'running', koban_session: session })).not.toContain('捞到')
  })
  it('preserves a confirmed zero instead of treating it as missing', () => {
    expect(runPostText({ status: 'completed', koban_session: { ...session, delta: 0 } })).toContain('0 小判')
  })
})

const ts = Date.parse('2026-10-01T19:00:00+08:00') / 1000
describe('honmaru journal facts', () => {
  it('combines nearby forge receipts without losing repeats or counting duplicate receipts', () => {
    const first = { id: 1, ts, event_type: 'forge.collected', payload: { receipt_key: 'a', swords: [{ name: '今剑' }, { name: '今剑' }] } }
    const second = { id: 2, ts: ts + 10, event_type: 'forge.collected', payload: { receipt_key: 'b', swords: [{ name: '岩融' }] } }
    const posts = buildJournalPosts([second, first, first])
    expect(posts).toHaveLength(1)
    expect(posts[0]!.text).toContain('3 振')
    expect(posts[0]!.facts).toEqual(['今剑 ×2', '岩融'])
    expect(posts[0]!.author).toBe('刀匠')
    expect(posts[0]!.firstObtained).toBe(false)
    expect(buildJournalPosts([first, second])[0]!.key).toBe(posts[0]!.key)
  })
  it('celebrates only client-confirmed first acquisitions, including manual collection', () => {
    const receipt = { id: 7, ts, script: 'youzu_log', event_type: 'forge.collected', payload: { receipt_key: 'manual', swords: [
      { name: '火车切', serial_id: 123, is_first_get_sword: true },
      { name: '今剑', serial_id: 124, is_first_get_sword: false },
    ] } }
    const post = buildJournalPosts([receipt, receipt])[0]!
    expect(post.author).toBe('刀匠')
    expect(post.title).toBe('火车切，锻出来了！')
    expect(post.text).toContain('2 振')
    expect(post.avatar).toContain('forge-smith-avatar')
    expect(post.firstObtained).toBe(true)
    expect(buildJournalPosts([{ ...receipt, payload: { name: '火车切' } }])[0]!.firstObtained).toBe(false)
  })
  it('keeps different maps and separate batches distinct', () => {
    const drop = (id: number, time: number, map: string) => ({ id, ts: time, event_type: 'sword.obtained', payload: { name: '今剑', chapter: '1', map_no: map } })
    expect(buildJournalPosts([drop(1, ts, '1'), drop(2, ts + 1, '2'), drop(3, ts + 301, '1')])).toHaveLength(3)
  })
  it('groups consumption by confirmed materials, ignoring unsupported reasons', () => {
    const posts = buildJournalPosts([], [
      { serial_id: 1, ts, reason: '链结' }, { serial_id: 2, ts, reason: '链结' },
      { serial_id: 3, ts: ts + 10, reason: '习合' }, { serial_id: 4, ts: ts + 20, reason: '刀解' },
      { serial_id: 5, ts, reason: '不知道' },
    ])
    expect(posts).toHaveLength(1)
    expect(posts[0]!.facts).toEqual(['链结 2 振', '习合 1 振', '刀解 1 振'])
  })
  it('shows inbox origin and expedition rewards without inventing unknown amounts', () => {
    const posts = buildJournalPosts([
      { id: 1, ts, event_type: 'sword.inbox_received', payload: { name: '火车切', origin_label: '任务奖励' } },
      { id: 2, ts: ts + 10, event_type: 'expedition.settled', payload: { team_no: 5, rewards: { 冷却材: 100, 小判: null } } },
      { id: 3, ts, event_type: 'expedition.dispatched', payload: { team_no: 4 } },
    ])
    expect(posts).toHaveLength(2)
    expect(posts[0]!.title).toContain('第 5 部队')
    expect(posts[0]!.facts).toEqual(['冷却材 +100'])
    expect(posts[1]!.facts).toEqual(['任务奖励', '火车切'])
  })
})

it('uses receipt captains and separates changed captains in the same team', () => {
  const receipt = (id: number, serial: number) => ({ id, ts, event_type: 'expedition.settled', payload: {
    team_no: 5, map_code: 'C4', captain: { name: '小豆长光·极', serial_id: serial, sword_id: 149 }, rewards: { 砥石: 750 },
  } })
  const posts = buildJournalPosts([receipt(1, 1), receipt(2, 2)])
  expect(posts).toHaveLength(2)
  expect(posts[0]!.author).toBe('小豆长光·极')
  expect(posts[0]!.avatar).toBe('/api/journal/avatar/149')
  expect(posts[0]!.text).toContain('天下布武远征回来了')
  expect(posts[0]!.facts).toEqual(['砥石 +750'])
})
