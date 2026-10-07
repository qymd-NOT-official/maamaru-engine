import { afterEach, expect, it, vi } from 'vitest'
import { api } from './api'

afterEach(() => vi.unstubAllGlobals())

it('routes JP homepage reads and editable profile/notes to JP only', async () => {
  const fetcher = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }))
  vi.stubGlobal('fetch', fetcher)
  await api.honmaruHome('jp')
  await api.honmaruSituation('jp')
  await api.refreshHonmaruSituation('jp')
  await api.saveHonmaruProfile({ honmaru_name: '日服', saniwa_name: '', province: '', attendant: '', motto: '', joined_on: '', avatar: '' }, 'jp')
  await api.saveHonmaruNote('日服小记', undefined, 'jp')
  await api.saveHonmaruNote('修改小记', 'note-id', 'jp')
  expect(fetcher.mock.calls.map(call => String((call as unknown[])[0]))).toEqual([
    '/api/honmaru-home?server=jp', '/api/honmaru-home/situation?server=jp',
    '/api/honmaru-home/situation/refresh?server=jp', '/api/honmaru-home/profile?server=jp',
    '/api/honmaru-home/notes?server=jp', '/api/honmaru-home/notes/note-id?server=jp',
  ])
})
