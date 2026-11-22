/** 跑况时间线解析器 —— 把任务日志行还原成「第几步、几点跑、跑了多久、成色如何」。
 *  纯函数不碰 DOM，方便单测（照 templateLabSelection.ts 的先例）。
 *
 *  数据来源与 LogPanel 完全同一份（/api/logs 与 /api/logs/stream），
 *  每行 {id, ts, run_id, script, message}；run 边界靠 run_id 分组，
 *  终态靠 script_runner 的收尾行（[脚本] 完成/已手动停止/…— run xxx）。
 */

export interface LogEntry { id: number; ts: number; run_id: string; script: string; message: string }

export type StepStatus = 'info' | 'ok' | 'warn' | 'skip' | 'fail'
export type RunEndStatus = 'completed' | 'stopped' | 'watchdog' | 'failed' | 'waiting'

export interface TimelineLine { id: number; ts: number; message: string; status: StepStatus }

export interface TimelineStep {
  tag: string
  title: string
  startTs: number
  endTs: number
  status: StepStatus
  lines: TimelineLine[]
}

export interface TimelineRun {
  runId: string
  script: string
  startTs: number
  endTs: number | null
  ended: boolean
  endStatus: RunEndStatus | null
  steps: TimelineStep[]
  lineCount: number
}

/** 状态严重程度：翻车 > 警告 > 跳过 > 成功 > 纯信息，步骤取行内最严重 */
const SEVERITY: Record<StepStatus, number> = { info: 0, ok: 1, skip: 2, warn: 3, fail: 4 }

/** 视觉模式里就该藏起来的噪音前缀（LogPanel 同款规矩） */
const NOISE_TAGS = new Set(['NAV', 'ADB', 'MAA'])
/** 调度器的提醒不是任务 run，不进时间线 */
const SCHEDULER_RUN_ID = 'scheduler'
/** script_runner._pump 的收尾行：一次 run 的句号 */
const RUN_END_RE = /^\[脚本\]\s*(完成|等待指定时间|已手动停止|看门狗已处决|工人进程异常退出|MAA 连续超时|玩法遇到异常)/
/** 流程/工作流的成绩单分隔线，出现即进入「总成绩单」步骤 */
const SCOREBOARD_RE = /={5,}/
/** 步骤横幅：流程工坊「[名] ▶ 第 3/5 步：label」、工作流「【工作流】▶ 第 1 块：label」 */
const STEP_BANNER_RE = /^(?:\[([^\]]+)\]|【([^】]+)】)\s*▶\s*第\s*(\d+)\s*(?:\/\s*\d+\s*)?步?块?[:：]\s*(.+)$/
/** 横幅标题里的重试角标（（翻车重试 1/2））——标题用干净名字 */
const RETRY_SUFFIX_RE = /（翻车重试[^）]*）\s*$/
const TAG_RE = /^(?:\[([^\]]+)\]|【([^】]+)】)\s*/

export const SCRIPT_NAMES: Record<string, string> = {
  daily: '一键日课', pumpkin: '南瓜', raid: '联队战', sortie: '合战场',
  yosari: '异去', osaka: '挖地', expedition: '远征', practice: '演练', smith: '锻刀',
  sakura: '刷花', sugar: '炼糖', repair: '手入', snapshot: '库存',
  rotate_captain: '换队长', formation: '编队换人', workflow: '工作流',
  apply_formation_preset: '套预设', custom_flow: '流程', scheduler: '排班', system: '系统',
}

export function scriptLabel(script: string) { return SCRIPT_NAMES[script] || script || '任务' }

export function lineStatus(message: string): StepStatus {
  const text = String(message || '')
  // ⏭ 先于翻车判：「跳过（翻车即停）」里带翻车俩字，但它是没轮到的 benign 记号
  if (/⏭|跳过（翻车即停）/.test(text)) return 'skip'
  if (/✗|🛑|翻车|异常退出/.test(text)) return 'fail'
  if (/⚠/.test(text)) return 'warn'
  if (/✓|✅/.test(text)) return 'ok'
  return 'info'
}

function endStatusOf(message: string): RunEndStatus {
  if (/等待指定时间/.test(message)) return 'waiting'
  if (/已手动停止/.test(message)) return 'stopped'
  if (/看门狗已处决/.test(message)) return 'watchdog'
  if (/异常退出|MAA 连续超时|玩法遇到异常/.test(message)) return 'failed'
  return 'completed'
}

function tagOf(message: string): string {
  const match = TAG_RE.exec(message)
  return match ? (match[1] ?? match[2] ?? '') : ''
}

function cleanMessage(message: string): string {
  return message.replace(TAG_RE, '').trim()
}

function truncate(text: string, max = 46): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}

export function parseRun(runId: string, script: string, entries: LogEntry[]): TimelineRun {
  const sorted = [...entries].sort((a, b) => a.id - b.id)
  const steps: TimelineStep[] = []
  let ended = false
  let endStatus: RunEndStatus | null = null
  let endTs: number | null = null
  let inScoreboard = false
  // 横幅开的步骤（流程工坊的「第 x/n 步」/ 工作流的「第 n 块」）是强归属：
  // 步骤自己的原始输出可能带别的 tag（[签到]、【NAV】之外的玩法前缀），
  // 在下一个横幅/成绩单/run 收尾出现前都归这一步，不许按 tag 切开。
  let bannerLocked = false

  const openStep = (tag: string, title: string, ts: number, locked = false) => {
    const step: TimelineStep = { tag, title, startTs: ts, endTs: ts, status: 'info', lines: [] }
    steps.push(step)
    bannerLocked = locked
    return step
  }
  let current: TimelineStep | null = null

  for (const entry of sorted) {
    const message = String(entry.message || '')
    const endMatch = RUN_END_RE.exec(message)
    if (endMatch) {
      ended = true
      endStatus = endStatusOf(message)
      endTs = entry.ts
      current = null
      bannerLocked = false
      continue
    }
    const tag = tagOf(message)
    if (NOISE_TAGS.has(tag)) continue

    // 成绩单分隔线：收拢当前步骤，后面所有行（含带前缀的收尾播报）都进「总成绩单」
    if (SCOREBOARD_RE.test(message)) {
      inScoreboard = true
      current = null
      bannerLocked = false
      continue
    }

    // 步骤横幅：流程工坊的「第 x/n 步：label」/ 工作流的「第 n 块：label」
    const banner = STEP_BANNER_RE.exec(message)
    if (banner && !inScoreboard) {
      const label = banner[4].replace(RETRY_SUFFIX_RE, '').trim()
      current = openStep(banner[1] ?? banner[2] ?? tag, label, entry.ts, true)
    } else if (inScoreboard) {
      if (!current) current = openStep('', '总成绩单', entry.ts)
    } else if (bannerLocked && current) {
      // 横幅步内：任何行都并入当前步
    } else if (!current || (tag && tag !== current.tag)) {
      // 无横幅的老流程（日课等）：连续同 tag 归一步，换 tag 开新步
      const text = cleanMessage(message).replace(/^▶\s*/, '')
      current = openStep(tag, truncate(text || message), entry.ts)
    }

    const status = lineStatus(message)
    current.lines.push({ id: entry.id, ts: entry.ts, message, status })
    current.endTs = entry.ts
    if (SEVERITY[status] > SEVERITY[current.status]) current.status = status
  }

  return {
    runId,
    script,
    startTs: sorted.length ? sorted[0].ts : 0,
    endTs,
    ended,
    endStatus,
    steps,
    lineCount: sorted.length,
  }
}

export function parseRuns(entries: LogEntry[]): TimelineRun[] {
  const sorted = [...entries].sort((a, b) => a.id - b.id)
  const groups = new Map<string, LogEntry[]>()
  const scripts = new Map<string, string>()
  for (const entry of sorted) {
    if (entry.run_id === SCHEDULER_RUN_ID) continue
    let group = groups.get(entry.run_id)
    if (!group) {
      group = []
      groups.set(entry.run_id, group)
      scripts.set(entry.run_id, entry.script)
    }
    group.push(entry)
  }
  return [...groups.entries()].map(([runId, group]) => parseRun(runId, scripts.get(runId) || '', group))
}

export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  if (total < 10) return `${(Math.max(0, ms) / 1000).toFixed(1)} 秒`
  if (total < 60) return `${total} 秒`
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  if (minutes < 60) return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  return `${hours} 小时 ${minutes % 60} 分`
}

export function formatClock(ts: number): string {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : ''
}
