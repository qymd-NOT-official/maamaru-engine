// 活动开幕参谋的表单模型：localStorage 记忆、入参组装、评级配色元数据。
// 纯函数不碰组件，测试照 dailySettingsModel 的装置风格来。
import type { BriefingFormationSaveBody, EventBriefingResponse } from '../../types'

export const EVENT_BRIEFING_STORAGE_KEY = 'maamaru.eventBriefing.form'

export interface EventBriefingFormData {
  name: string
  damage: 'virtual' | 'real'
  battle: 'day' | 'night' | 'indoor' | 'mixed'
  /** 文本输入原样保存；空串 = 不知道，请求里不填 */
  mapPoints: string
  recommendedLevel: string
  bonusSwords: string[]
  enemyFastSpear: boolean
}

export const DAMAGE_OPTIONS = [
  { value: 'virtual', label: '虚拟伤害（活动刀随便带）' },
  { value: 'real', label: '真伤（等级不够会真受伤）' },
] as const

export const BATTLE_OPTIONS = [
  { value: 'day', label: '日战' },
  { value: 'night', label: '夜战' },
  { value: 'indoor', label: '室内战' },
  { value: 'mixed', label: '日夜混合' },
] as const

export type BonusVerdict = EventBriefingResponse['bonus'][number]['verdict']

// 评级分色：good=放心带 / warn=凑合 / bad=别带 / dim=信息不足
export const BONUS_VERDICT_META: Record<BonusVerdict, { label: string; tone: 'good' | 'warn' | 'bad' | 'dim' }> = {
  carry: { label: '带得动', tone: 'good' },
  free: { label: '白嫖位', tone: 'good' },
  marginal: { label: '勉强能带', tone: 'warn' },
  burden: { label: '拖累队伍', tone: 'bad' },
  missing: { label: '刀帐没有', tone: 'dim' },
  unknown: { label: '等级存疑', tone: 'dim' },
}

export function defaultBriefingForm(): EventBriefingFormData {
  return {
    name: '',
    damage: 'virtual',
    battle: 'day',
    mapPoints: '',
    recommendedLevel: '',
    bonusSwords: [],
    enemyFastSpear: false,
  }
}

function storageOrNull(storage?: Storage): Storage | null {
  if (storage) return storage
  try {
    return globalThis.localStorage ?? null
  } catch {
    return null
  }
}

export function loadBriefingForm(storage?: Storage): EventBriefingFormData {
  const store = storageOrNull(storage)
  const fallback = defaultBriefingForm()
  if (!store) return fallback
  try {
    const raw = store.getItem(EVENT_BRIEFING_STORAGE_KEY)
    if (!raw) return fallback
    const data = JSON.parse(raw)
    if (!data || typeof data !== 'object') return fallback
    return {
      name: typeof data.name === 'string' ? data.name : '',
      damage: data.damage === 'real' ? 'real' : 'virtual',
      battle: ['day', 'night', 'indoor', 'mixed'].includes(data.battle) ? data.battle : 'day',
      mapPoints: typeof data.mapPoints === 'string' ? data.mapPoints : '',
      recommendedLevel: typeof data.recommendedLevel === 'string' ? data.recommendedLevel : '',
      bonusSwords: Array.isArray(data.bonusSwords)
        ? data.bonusSwords
            .map((name: unknown) => (typeof name === 'string' ? name.trim() : ''))
            .filter(Boolean)
        : [],
      enemyFastSpear: data.enemyFastSpear === true,
    }
  } catch {
    return fallback
  }
}

export function saveBriefingForm(form: EventBriefingFormData, storage?: Storage): void {
  const store = storageOrNull(storage)
  if (!store) return
  try {
    store.setItem(EVENT_BRIEFING_STORAGE_KEY, JSON.stringify(form))
  } catch {
    // 存不下就不存：参谋是参谋，不能因记忆失败卡住出阵建议
  }
}

/** 文本框 → 整数；空/非数字/越界一律 null（不知道就别填，后端也这么验） */
export function parseOptionalInt(raw: string): number | null {
  const text = raw.trim()
  if (!text) return null
  const value = Number(text)
  if (!Number.isInteger(value) || value < 1 || value > 99) return null
  return value
}

/** 表单 → POST /api/event-briefing 平铺入参（活动字段 + 敌方快枪） */
export function briefingRequestBody(form: EventBriefingFormData): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name: form.name.trim(),
    damage: form.damage,
    battle: form.battle,
    bonus_swords: form.bonusSwords.map(name => name.trim()).filter(Boolean),
  }
  const mapPoints = parseOptionalInt(form.mapPoints)
  if (mapPoints != null) body.map_points = mapPoints
  const recommendedLevel = parseOptionalInt(form.recommendedLevel)
  if (recommendedLevel != null) body.recommended_level = recommendedLevel
  return body
}

export function hasWarnings(briefing: EventBriefingResponse | null): boolean {
  return !!briefing && briefing.warnings.length > 0
}

// 疲劳档位与规则表 fatigue.tiers 对齐；未知不猜
export function fatigueLabel(fatigue: number | null): string {
  if (fatigue == null) return '疲劳未知'
  if (fatigue >= 50) return '樱吹雪'
  if (fatigue >= 20) return '通常'
  if (fatigue >= 6) return '黄脸'
  return '红脸'
}

export function formLabel(form: 'kiwame' | 'normal'): string {
  return form === 'kiwame' ? '极' : '普通'
}

// ---- 编队候选 → 部队预设 ----
// 名字契约与后端 panel/event_briefing_api.briefing_preset_name 保持一致：
// 「活动·<活动名>」总长按 20 字截断；重名由后端覆盖旧预设。

export const PRESET_TEAM_OPTIONS = [
  { value: 1, label: '部队一' },
  { value: 2, label: '部队二' },
  { value: 3, label: '部队三' },
  { value: 4, label: '部队四' },
  { value: 5, label: '部队五' },
] as const

export function briefingPresetName(eventName: string): string {
  const base = eventName.trim()
  return (base ? `活动·${base}` : '活动编队').slice(0, 20)
}

/** 简报编队候选（最多 6 振）→ POST /api/event-briefing/formation 入参 */
export function formationSaveBody(
  briefing: EventBriefingResponse,
  eventName: string,
  targetTeam: number,
): BriefingFormationSaveBody {
  return {
    event_name: eventName.trim(),
    target_team: targetTeam,
    candidates: briefing.formation.candidates.slice(0, 6)
      .map(candidate => ({ name: candidate.name, form: candidate.form, level: candidate.level })),
  }
}

/** 存预设按钮能不能点：没候选就是没东西可存 */
export function canSaveFormation(briefing: EventBriefingResponse | null): boolean {
  return !!briefing && briefing.formation.candidates.length > 0
}
