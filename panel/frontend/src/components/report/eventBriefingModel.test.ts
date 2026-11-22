import { afterEach, describe, expect, it, vi } from 'vitest'
import type { EventBriefingResponse } from '../../types'
import { api } from '../../api'
import {
  BONUS_VERDICT_META,
  EVENT_BRIEFING_STORAGE_KEY,
  briefingPresetName,
  briefingRequestBody,
  canSaveFormation,
  defaultBriefingForm,
  fatigueLabel,
  formLabel,
  formationSaveBody,
  hasWarnings,
  loadBriefingForm,
  parseOptionalInt,
  saveBriefingForm,
  type EventBriefingFormData,
} from './eventBriefingModel'

function memoryStorage(initial: Record<string, string> = {}) {
  const map = new Map(Object.entries(initial))
  return {
    getItem: (key: string) => (map.has(key) ? map.get(key)! : null),
    setItem: (key: string, value: string) => { map.set(key, value) },
    removeItem: (key: string) => { map.delete(key) },
    dump: () => Object.fromEntries(map),
  } as Storage & { dump: () => Record<string, string> }
}

function filledForm(over: Partial<EventBriefingFormData> = {}): EventBriefingFormData {
  return {
    ...defaultBriefingForm(),
    name: '战力扩充计划',
    damage: 'real',
    battle: 'night',
    mapPoints: '5',
    recommendedLevel: '60',
    bonusSwords: ['后家兼光', '拔丸'],
    ...over,
  }
}

afterEach(() => vi.unstubAllGlobals())

describe('表单 localStorage 记忆', () => {
  it('没有记忆时给默认表单', () => {
    expect(loadBriefingForm(memoryStorage())).toEqual(defaultBriefingForm())
  })

  it('存进去能原样读回来', () => {
    const storage = memoryStorage()
    saveBriefingForm(filledForm(), storage)
    expect(loadBriefingForm(storage)).toEqual(filledForm())
  })

  it('存坏的内容不炸，回默认表单', () => {
    const storage = memoryStorage({ [EVENT_BRIEFING_STORAGE_KEY]: '{oops' })
    expect(loadBriefingForm(storage)).toEqual(defaultBriefingForm())
    const weird = memoryStorage({ [EVENT_BRIEFING_STORAGE_KEY]: '"just a string"' })
    expect(loadBriefingForm(weird)).toEqual(defaultBriefingForm())
  })

  it('读回来的字段做类型消毒：加成刀名单只留非空字符串', () => {
    const storage = memoryStorage({
      [EVENT_BRIEFING_STORAGE_KEY]: JSON.stringify({
        name: 42, damage: 'real', battle: 'space',
        mapPoints: 5, bonusSwords: [' 后家兼光 ', '', 7, null],
        enemyFastSpear: 'yes',
      }),
    })
    const form = loadBriefingForm(storage)
    expect(form.name).toBe('')
    expect(form.damage).toBe('real')
    expect(form.battle).toBe('day')  // 非法战场类型回日战
    expect(form.mapPoints).toBe('')
    expect(form.bonusSwords).toEqual(['后家兼光'])
    expect(form.enemyFastSpear).toBe(false)
  })
})

describe('可选数字输入', () => {
  it('空串和越界一律 null（不知道就别填）', () => {
    expect(parseOptionalInt('')).toBeNull()
    expect(parseOptionalInt('   ')).toBeNull()
    expect(parseOptionalInt('abc')).toBeNull()
    expect(parseOptionalInt('5.5')).toBeNull()
    expect(parseOptionalInt('0')).toBeNull()
    expect(parseOptionalInt('100')).toBeNull()
  })

  it('合法整数原样返回', () => {
    expect(parseOptionalInt('5')).toBe(5)
    expect(parseOptionalInt(' 99 ')).toBe(99)
  })
})

describe('生成简报的请求体', () => {
  it('空着的可选项不塞进请求', () => {
    const body = briefingRequestBody(filledForm({ mapPoints: '', recommendedLevel: '' }))
    expect(body).toEqual({
      name: '战力扩充计划',
      damage: 'real',
      battle: 'night',
      bonus_swords: ['后家兼光', '拔丸'],
    })
  })

  it('填了的可选项带进去，加成刀去掉空白项', () => {
    const body = briefingRequestBody(filledForm({ bonusSwords: [' 后家兼光 ', ''] }))
    expect(body.map_points).toBe(5)
    expect(body.recommended_level).toBe(60)
    expect(body.bonus_swords).toEqual(['后家兼光'])
  })
})

describe('评级配色与警示元数据', () => {
  it('后端六种评级都有文案和配色档', () => {
    for (const verdict of ['carry', 'free', 'marginal', 'burden', 'missing', 'unknown']) {
      expect(BONUS_VERDICT_META[verdict as keyof typeof BONUS_VERDICT_META].label).toBeTruthy()
      expect(['good', 'warn', 'bad', 'dim']).toContain(
        BONUS_VERDICT_META[verdict as keyof typeof BONUS_VERDICT_META].tone)
    }
  })

  function briefingWith(warnings: string[]): EventBriefingResponse {
    return { summary: '', bonus: [], formation: { template_note: '', wanted_types: [], candidates: [], train_note: null }, troops: [], flower: [], warnings, event: { name: '', damage: 'virtual', battle: 'day', map_points: null, recommended_level: null, bonus_swords: [], notes: '' } }
  }

  it('有重伤/红脸警告时警示区必须亮出来', () => {
    expect(hasWarnings(briefingWith(['小夜左文字 红脸（疲劳 12）']))).toBe(true)
    expect(hasWarnings(briefingWith([]))).toBe(false)
    expect(hasWarnings(null)).toBe(false)
  })

  it('疲劳档位对齐规则表：不知道不猜', () => {
    expect(fatigueLabel(88)).toBe('樱吹雪')
    expect(fatigueLabel(50)).toBe('樱吹雪')
    expect(fatigueLabel(49)).toBe('通常')
    expect(fatigueLabel(20)).toBe('通常')
    expect(fatigueLabel(19)).toBe('黄脸')
    expect(fatigueLabel(6)).toBe('黄脸')
    expect(fatigueLabel(5)).toBe('红脸')
    expect(fatigueLabel(null)).toBe('疲劳未知')
  })

  it('形态标签：kiwame=极，其余=普通', () => {
    expect(formLabel('kiwame')).toBe('极')
    expect(formLabel('normal')).toBe('普通')
  })
})

describe('API 接线', () => {
  it('POST /api/event-briefing：活动字段平铺 + 敌方快枪开关', async () => {
    const fetcher = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }))
    vi.stubGlobal('fetch', fetcher)
    await api.eventBriefing({ name: '江户城', damage: 'virtual', battle: 'indoor', bonus_swords: [] }, true)
    expect(String((fetcher.mock.calls[0] as unknown[])[0])).toBe('/api/event-briefing')
    const init = (fetcher.mock.calls[0] as unknown[])[1] as RequestInit
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect(JSON.parse(String(init.body))).toEqual({
      name: '江户城', damage: 'virtual', battle: 'indoor', bonus_swords: [],
      enemy_fast_spear: true,
    })
  })

  it('敌方快枪默认关', async () => {
    const fetcher = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }))
    vi.stubGlobal('fetch', fetcher)
    await api.eventBriefing({ name: '秘宝之里', damage: 'virtual', battle: 'day', bonus_swords: [] })
    const init = (fetcher.mock.calls[0] as unknown[])[1] as RequestInit
    expect(JSON.parse(String(init.body)).enemy_fast_spear).toBe(false)
  })

  it('后端 503（刀帐没盘点）的引导文案原样抛给界面', async () => {
    const fetcher = vi.fn(async () => ({
      ok: false, status: 503,
      json: async () => ({ detail: '刀帐还没有可用数据：先去「刀剑」页跑一次「刀剑盘点」' }),
    }))
    vi.stubGlobal('fetch', fetcher)
    await expect(api.eventBriefing({ name: 'x', damage: 'virtual', battle: 'day', bonus_swords: [] }))
      .rejects.toThrow('刀剑盘点')
  })
})

describe('编队候选存预设', () => {
  function briefingWithCandidates(count: number): EventBriefingResponse {
    const candidates = Array.from({ length: count }, (_, index) => ({
      name: `刀${index + 1}`, type: '打刀', level: 90 + index,
      form: 'normal' as const, fatigue: 50, injury: null, slots: 3,
    }))
    return {
      summary: '', bonus: [],
      formation: { template_note: '', wanted_types: [], candidates, train_note: null },
      troops: [], flower: [], warnings: [],
      event: { name: '战力扩充计划', damage: 'virtual', battle: 'day', map_points: null, recommended_level: null, bonus_swords: [], notes: '' },
    }
  }

  it('预设名：活动·<活动名>，空白兜底，20 字截断', () => {
    expect(briefingPresetName(' 战力扩充计划 ')).toBe('活动·战力扩充计划')
    expect(briefingPresetName('')).toBe('活动编队')
    expect(briefingPresetName('超长活动名'.repeat(10))).toHaveLength(20)
  })

  it('存预设入参：候选最多 6 振，只带 name/form/level', () => {
    const body = formationSaveBody(briefingWithCandidates(8), ' 江户城 ', 3)
    expect(body.event_name).toBe('江户城')
    expect(body.target_team).toBe(3)
    expect(body.candidates).toHaveLength(6)
    expect(body.candidates[0]).toEqual({ name: '刀1', form: 'normal', level: 90 })
  })

  it('没候选不能存', () => {
    expect(canSaveFormation(briefingWithCandidates(0))).toBe(false)
    expect(canSaveFormation(briefingWithCandidates(2))).toBe(true)
    expect(canSaveFormation(null)).toBe(false)
  })

  it('POST /api/event-briefing/formation：入参原样发出', async () => {
    const fetcher = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ ok: true, formation: {}, overwritten: false }) }))
    vi.stubGlobal('fetch', fetcher)
    await api.saveBriefingFormation({ event_name: '江户城', target_team: 2, candidates: [{ name: '药研藤四郎', form: 'kiwame', level: 99 }] })
    expect(String((fetcher.mock.calls[0] as unknown[])[0])).toBe('/api/event-briefing/formation')
    const init = (fetcher.mock.calls[0] as unknown[])[1] as RequestInit
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({
      event_name: '江户城', target_team: 2,
      candidates: [{ name: '药研藤四郎', form: 'kiwame', level: 99 }],
    })
  })

  it('存预设失败（后端 400）的原因原样抛给界面', async () => {
    const fetcher = vi.fn(async () => ({
      ok: false, status: 400,
      json: async () => ({ reason: '「薬研（极）」在刀帐里有 2 振同名同形态，分不清是哪一振' }),
    }))
    vi.stubGlobal('fetch', fetcher)
    await expect(api.saveBriefingFormation({ event_name: 'x', target_team: 1, candidates: [] }))
      .rejects.toThrow('分不清')
  })
})
