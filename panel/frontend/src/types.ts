export type Option = string | [string, string]

export interface HonmaruProfile {
  honmaru_name: string
  saniwa_name: string
  province: string
  attendant: string
  motto: string
  joined_on: string
  avatar: string
}
export interface HonmaruNote { id: string; body: string; created_at: number; updated_at?: number }
export interface HonmaruHomeData { schema_version: number; profile: Partial<HonmaruProfile>; notes: HonmaruNote[] }
export interface HonmaruSituationMember {
  name: string
  level: number
  /** 同名消歧标签（「·Lv99」「·2号机」），展示优先于 name */
  label?: string
  serial_tail?: string
  hp?: number | null
  hp_max?: number | null
  fatigue?: number | null
  injury?: '轻伤' | '中伤' | '重伤' | null
}
export interface HonmaruSituation {
  schema: number
  secretary: { name: string; observed_at: string | null }
  parties: Array<{ party_no: number; party_name: string; members: HonmaruSituationMember[]; finished_at: string | null }>
  parties_observed_at: string | null
  kiwame_return: Array<{ name: string; finished_at: string }>
  kiwame_observed_at: string | null
  forge_slots: Array<{ slot_no: number; finished_at: string }>
  forge_observed_at: string | null
  /** /home 真实读数；日志没进过本丸时为 null，不冒充 */
  resources?: { charcoal: number; steel: number; coolant: number; whetstone: number; bill: number; koban: number } | null
  resources_observed_at?: string | null
  /** 手入槽（结构按实拍透传，有 serial 能认出名字才带 name） */
  repair?: Array<{ slot_no: number; finished_at: string | null; name?: string }>
  repair_observed_at?: string | null
  duty?: { finished_at: string | null } | null
  duty_observed_at?: string | null
  /** 活动点数（event_id→活动名未校准，只展示数字） */
  event_points?: Array<{ event_id: string; points: number }>
  event_points_observed_at?: string | null
}

export interface VisibilityRule {
  key?: string
  is?: string
  not?: string
  is_any?: string[]
  all?: VisibilityRule[]
  any?: VisibilityRule[]
}

export interface ParamField {
  key: string
  type: 'select' | 'number' | 'text' | 'checks' | 'note' | 'toggle' | 'duration-list' | 'template' | 'roi' | 'step'
  label?: string
  default?: unknown
  options?: Option[]
  min?: number
  max?: number
  help?: string
  text?: string
  placeholder?: string
  swords?: boolean
  visibleWhen?: VisibilityRule
}

export interface ScriptInfo {
  label: string
  desc: string
  params: ParamField[]
}

export interface ScriptsResponse {
  running: boolean
  current: string | null
  run_id?: string | null
  workflow?: WorkflowIdentity | null
  scripts: Record<string, ScriptInfo>
  event_hidden?: string[]
}

export interface WorkflowIdentity { id: string; name: string }

// ---- 执务页常用功能自定义 /api/home-layout ----

export interface HomeLayoutEntry {
  kind: 'script' | 'workflow'
  key: string
  label: string
}

export interface HomeLayout {
  order: string[]
  hidden: string[]
  entries: HomeLayoutEntry[]
}

// ---- 自定义工作流 /api/workflows ----

export interface WorkflowNode {
  type: string
  params: ScriptParams
  on_error: 'stop' | 'continue'
}

export interface WorkflowProjectionStep {
  label: string
  type: string
  at: number | null
  wait_time: string | null
}

export interface WorkflowPreset {
  steps?: WorkflowProjectionStep[]
  id: string
  name: string
  nodes: WorkflowNode[]
  after?: 'none' | 'logout' | 'shutdown' | 'sleep'
  daily_mode?: boolean
}

export type WorkflowNodeCategory = 'cold' | 'time' | 'chore' | 'battle' | 'finish'

export interface WorkflowNodeDef {
  type: string
  label: string
  desc: string
  category: WorkflowNodeCategory
  params: ParamField[]
  saved_params?: ScriptParams
  template_only?: boolean
}

export type ScriptParams = Record<string, unknown>

// ---- 本丸成绩单 /api/data/resource-ledger ----

export interface ClientAsset {
  serial_id?: string | number
  name?: string
  kind?: 'troop' | 'horse'
  equip_id?: string | number
  artifact_id?: string | number
  level?: string | number
  sword_id?: string | number
  charm_name?: string
  item_id?: string | number
  horse_serial_id?: string | number
  equip_serial_id1?: string | number
  equip_serial_id2?: string | number
  equip_serial_id3?: string | number
  artifact_serial_id1?: string | number
  artifact_serial_id2?: string | number
}
export interface ClientInventory {
  items: Record<string, { count: number; observed_at: number }>
  resources: Record<string, { count: number; observed_at: number; source?: 'screen' }>
  koban_reserve: number | null
  assets: { equip?: ClientAsset[]; artifact?: ClientAsset[]; sword?: ClientAsset[]; observed_at: number } | null
}

export interface LedgerAttribution {
  run_label?: string
  raw_source?: string
  execution_script?: string
  evidence_ids?: number[]
  id: string
  ts: number
  resource: string
  delta: number
  source: string
  script?: string
  run_id?: string
  event_id?: number
  label?: string
  confidence?: string
}

export interface LedgerDay {
  date: string
  resource: string
  opening?: number | null
  closing?: number | null
  total_delta: number | null
  attributed_delta?: number
  unattributed_delta?: number | null
  observation_count?: number
  confidence?: string
  attribution_ids?: string[]
  gap_ids?: string[]
}

// 余额折线图的取点：某时刻各资源的余额读数（缺的资源不补零）
export interface BalancePoint {
  ts: number
  date: string
  values: Record<string, number>
}

export interface LedgerResource {
  resource: string
  opening: number | null
  closing: number | null
  total_delta: number | null
  attributed_delta: number
  unattributed_delta: number | null
  observation_count: number
  confidence: string
}

export interface ResourceLedger {
  schema_version: number
  generated_at: number
  window: { from: number; to: number; timezone: string; days: number }
  per_resource: LedgerResource[]
  daily_series: LedgerDay[]
  balance_series?: BalancePoint[]
  gaps: any[]
  attributions: LedgerAttribution[]
  unresolved_changes?: LedgerAttribution[]
}

export interface HumanReport {
  id: number
  occurred_at: number
  activities?: string[]
  note?: string
  source?: string
  gap_key?: string | null
  resource?: string | null
  claimed_delta?: number | null
  group_id?: string | null
}

export interface ManualSession {
  id: number
  created_at: number
  script: string
  activity: string
  started_at: number
  ended_at: number
  loops: number
  duration_seconds: number
  average_loop_seconds: number
  note?: string
  source: 'manual'
}

export interface ManualInventory {
  id: number
  ts: number
  captured_at: string
  source: 'manual_entry' | 'manual_import'
  resources: Record<string, number>
}

export interface LedgerImportItem {
  kind: 'transaction' | 'inventory' | 'session'
  row: number
  status: 'new' | 'duplicate' | 'conflict'
  detail: string
  summary: string
}

export interface LedgerImportPreview {
  ok: boolean
  schema_version: number
  preview_id: string
  filename: string
  source_sha256: string
  counts: { new: number; duplicate: number; conflict: number; invalid: number; ignored: number }
  items: LedgerImportItem[]
  issues: Array<{ row: number; ignored: boolean; reason: string }>
}

// ---- 日服抓包导入 /api/data/jp-netlog-import ----

export interface JpNetlogImportResult {
  ok: boolean
  transactions: number
  stats: {
    'inventory.captured': number
    'training.captured': number
    'forge.started': number
    'forge.collected': number
    'resource.change': number
    skipped_encrypted: number
    skipped_no_now: number
    dedup_snapshots: number
  }
  summary: {
    kinds: Record<string, number>
    unknown_paths: Record<string, number>
    encrypted_battles: number
    resource: Record<string, number> | null
    swords: { count: number; sample: string[] }
  }
}

// ---- 日服实时听包 /api/jp-listener/* ----

export interface JpListenerStatus {
  state: 'off' | 'starting' | 'waiting_browser' | 'listening' | 'error'
  detail: string
  started_at: number | null
  last_capture_at: number | null
  /** 已听到的游戏报文数（含去重丢弃的） */
  transactions: number
  /** 实际落账的事件条数 */
  events_written: number
  /** 调试口活着（浏览器开着） */
  browser_alive?: boolean
  /** 当前挂上 Network 订阅的目标数（页面+iframe）；游戏 iframe 挂不上时这里能看出少了一只耳朵 */
  sessions?: number
}

export interface LedgerOnboarding {
  schema_version: number
  visible: boolean
  status: 'pending' | 'active' | 'completed' | 'dismissed' | 'not_needed'
  step: 1 | 2 | 3
  has_inventory: boolean
  reason: string
}

export interface ActivityPace {
  source: 'maamaru' | 'manual'
  secondsPerLoop: number
  loops: number
  runStartedAt: number
}

export interface InventoryGap {
  gap_key: string
  started_at: number
  ended_at: number
  resource_delta?: Record<string, number>
  reported?: boolean
}

// ---- 规划建议 /api/planning ----

export interface PlanningGoalAdvice {
  id: number
  kind?: 'resource' | 'event' | 'fragment'
  event?: string | null
  fragment?: string | null
  goal_mode?: 'budget' | 'stock_target' | 'combined' | 'amount_target' | 'deadline_target' | null
  resource: string
  target: number | null
  deadline: string | null
  estimated_deadline?: string | null
  deadline_at?: string | null
  note?: string
  days_left: number | null
  current: number | null
  rate: number | null
  projected: number | null
  shortfall: number | null
  extra_daily: number | null
  extra_floors: number | null
  expected_runs?: number | null
  best_map_label?: string | null
  fragment_rate?: number | null
  floors_needed?: number | null
  floors_per_day?: number | null
  seconds_per_floor?: number | null
  speed_sample_floors?: number | null
  estimated_seconds?: number | null
  remaining_seconds?: number | null
  time_margin_seconds?: number | null
  can_finish?: boolean | null
  planned_spending?: number | null
  impact_days?: number | null
  conflicting_goal?: string | null
  status: 'done' | 'on_track' | 'behind' | 'active' | 'expired' | 'unknown'
  message: string
}

export interface AcquisitionExpedition {
  map: string
  label: string
  name: string
  duration_min: number
  amount: number
  per_hour: number
  level_req: number | null
}

export interface AcquisitionGuide {
  resource: string
  expeditions: AcquisitionExpedition[]
  expedition_caveat?: string | null
  mission?: string | null
  event?: string | null
  note?: string | null
}

export interface FragmentMapRate {
  map_no: number
  label: string
  rate: number
}

export interface FragmentGuide {
  fragment: string
  maps: FragmentMapRate[]
  best_map: FragmentMapRate | null
}

export interface FragmentNotes {
  rate_source: string
  milestones: { runs: number; reward: string }[]
  milestone_progress?: {
    total_runs: number
    baseline_runs: number
    counted_after_baseline: number
    next_milestone: { runs: number; reward: string } | null
    remaining: number
  }
  campaign: { name: string; rate_multiplier?: number; start_at: string; end_at: string; active?: boolean | null } | null
}

export interface PlanningReport {
  schema_version: number
  generated_at: number
  today: string
  rate_window_days: number
  rates: Record<string, { daily: number | null; days_observed: number }>
  koban_per_floor: { per_floor: number; sessions: number } | null
  osaka_floor_speed?: { seconds_per_floor: number; floors: number; run_started_at?: number | null } | null
  resource_watch?: {
    resources: { resource: string; current: number | null; per_forge: number; forge_capacity: number | null }[]
    forge_capacity: number | null
    limiting: string[]
    ten_forge?: {
      resources: { resource: string; current: number | null; per_forge: number; forge_capacity: number | null }[]
      forge_capacity: number | null
      limiting: string[]
    }
  }
  client_inventory?: { items: Record<string, { count: number; observed_at: number }> }
  koban_watch?: {
    boxed_reserve?: number | null
    total_with_boxes?: number | null
    budgets?: { id: string; event: string; amount: number | null; source: string }[]
    current: number | null
    reserved: number
    available: number | null
    confirmed_spending: number
    spending_days: number
  }
  current?: Record<string, number | null>
  goals: PlanningGoalAdvice[]
  events?: EventAbacus[]
  acquisition?: Record<string, AcquisitionGuide>
  fragments?: Record<string, FragmentGuide>
  fragment_notes?: FragmentNotes
}

// ---- 日报 /api/daily_report ----

export interface DailyReportResourceEntry {
  ts: number
  resource: string
  delta: number
  label: string
  note: string
  source: string
  attribution: string
}

export interface DailyReportBalance {
  ts: number
  captured_at: string
  resources: Record<string, number>
}

export interface DailyReportResources {
  net: Record<string, number>
  groups: { label: string; source: string; attribution: string; count: number; net: Record<string, number> }[]
  opening: DailyReportBalance | null
  closing: DailyReportBalance | null
  entries: DailyReportResourceEntry[]
  entry_total: number
  truncated: number
}

export interface DailyReportDropSword {
  name: string
  is_first_get_sword: boolean
  ts: number
}

export interface DailyReportDropGroup {
  label: string
  swords: DailyReportDropSword[]
  count: number
  first_get_count: number
}

export interface DailyReportDrops {
  groups: DailyReportDropGroup[]
  total: number
  first_get_total: number
}

export interface DailyReportTrainingRow {
  name: string
  serial_id: number
  from: number | null
  to: number
}

export interface DailyReportExpRow {
  name: string
  serial_id: number
  exp_gain: number
  exp: number | null
}

export interface DailyReportTraining {
  snapshot_ts: number
  snapshot_captured_at: string
  previous_ts: number | null
  previous_captured_at: string | null
  has_previous: boolean
  sword_count: number
  level_ups: DailyReportTrainingRow[]
  ranbu_ups: DailyReportTrainingRow[]
  exp_top: DailyReportExpRow[]
}

export interface DailyReportGoal {
  id: number | null
  kind: string
  resource: string | null
  fragment: string | null
  event: string | null
  target: number | null
  deadline: string | null
  status: string
  message: string
  note: string
}

export interface DailyReportAttendance {
  run_id: string | null
  script: string | null
  label: string | null
  status: string | null
  started_at: number | null
  ended_at: number | null
}

export interface DailyReport {
  schema_version: number
  date: string
  timezone: string
  window: { start: number; end: number }
  resources: DailyReportResources | null
  drops: DailyReportDrops | null
  training: DailyReportTraining | null
  goals: DailyReportGoal[] | null
  attendance: DailyReportAttendance[] | null
  degraded: string[]
}

// ---- 活动日历 /api/events ----

export interface EventAnnouncement {
  title: string
  publish_time: number
  publish_date: string | null
  update_date: string | null
  events: string[]
  url: string | null
}

export interface EventsCalendar {
  generated_at?: number
  source?: string
  announcements: EventAnnouncement[]
  stale: boolean
  reason?: string
}

// ---- 活动算盘（/api/planning 的 events 字段） ----

export interface EventAbacus {
  event: string
  mechanics?: string | null
  currency?: string | null
  goal_mode?: 'budget' | 'stock_target'
  goal_resource?: string
  start_date: string | null
  end_date: string | null
  keys_total: number
  boxes: number | null
  ticket_price: number
  daily_free_tickets: number
  note: string
  keys_per_run: number | null
  keys_source: 'measured' | 'history' | 'estimate' | null
  keys_obtained?: number | null
  keys_remaining?: number | null
  runs_total?: number | null
  keys_basis?: string | null
  runs_needed: number | null
  free_runs: number | null
  paid_tickets: number | null
  koban_cost: number | null
  days_left: number | null
  available_now: number | null
  sufficient: boolean | null
  shortfall: number | null
  yield_per_floor?: number | null
  yield_sessions?: number | null
  tama_current?: number | null
  tama_observed_at?: number | null
  tama_target?: number | null
  tama_target_custom?: boolean
  tama_remaining?: number | null
  tama_per_loop?: number | null
  tama_samples?: number | null
  seconds_per_loop?: number | null
  estimated_seconds?: number | null
  seconds_to_end?: number | null
  can_finish?: boolean | null
  free_tickets_remaining?: number | null
  message: string
}

export interface EventGoalResult {
  ok: boolean
  sufficient: boolean | null
  goal: Record<string, unknown> | null
  goal_mode?: 'budget' | 'stock_target'
  target?: number
  koban_cost: number | null
  available_now: number | null
  shortfall: number | null
}

// ---- 事件时间轴 /api/events/timeline ----

export interface EventTimelineBudget {
  koban_cost: number | null
  available_now: number | null
  shortfall: number | null
  sufficient: boolean | null
  paid_tickets?: number | null
  runs_needed?: number | null
  free_runs?: number | null
  keys_obtained?: number | null
  mechanics?: string | null
  currency?: string | null
  tama_current?: number | null
  tama_observed_at?: number | null
  tama_target?: number | null
  tama_target_custom?: boolean
  tama_remaining?: number | null
  tama_per_loop?: number | null
  tama_samples?: number | null
  seconds_per_loop?: number | null
  estimated_seconds?: number | null
  seconds_to_end?: number | null
  can_finish?: boolean | null
  free_tickets_remaining?: number | null
  message: string
}

// ---- 所持刀剑（/api/data/sword-inventory/latest） ----

export interface SwordInventoryRow {
  sword_id: string
  name_zh: string
  level: number | null
  tou_level: number | null
  survival: number | null
  survival_max: number | null
  fatigue: number | null
  fatigue_max: number | null
  stats: Record<string, number | boolean>
  kiwame_date: string | null
  locked: boolean | null
  page_no: number | null
}

export interface SwordInventorySnapshot {
  id: number
  captured_at: number
  owned: number | null
  capacity: number | null
  sword_count: number
  missing: number | null
  swords: SwordInventoryRow[]
}

export interface SwordInventoryResponse {
  schema_version: number
  snapshot: SwordInventorySnapshot | null
}

export interface EventTimelineEntry {
  name: string
  precise: boolean
  start_at: string | null
  end_at: string | null
  start_date: string
  end_date: string | null
  note: string
  days_left: number | null
  days_until_start?: number
  budget: EventTimelineBudget | null
  summary?: EventPeriodSummary | null
}

// 刚收官活动的本期小结（来自 event_history 归档）
export interface EventPeriodSummary {
  mechanics: 'edocastle' | 'hanafuda' | 'raid' | null
  currency?: string | null
  runs: number | null
  keys_per_run?: number | null
  keys_total?: number | null
  full_clear?: boolean
  tama_per_run?: number | null
  total_tama?: number | null
  currency_per_run?: number | null
  currency_total?: number | null
  koban_spent: number | null
  period: string
}

export interface EventTimelineCandidate {
  name: string | null
  section: string | null
  start_at: string | null
  end_at: string | null
  announcement: string | null
  url: string | null
}

export interface EventTimelineReport {
  generated_at: string
  calendar_stale: boolean
  ongoing: EventTimelineEntry[]
  upcoming: EventTimelineEntry[]
  later: EventTimelineEntry[]
  ended?: EventTimelineEntry[]
  unverified: EventTimelineCandidate[]
}

// ---- 异常与通知中心 /api/incidents ----

export interface Incident {
  code: string
  severity: 'info' | 'warning' | 'urgent'
  title: string
  cause: string
  action: string
  needs_human: boolean
  entry: { tab?: string; script?: string }
  status: 'active' | 'acknowledged' | 'resolved'
  first_seen: number
  last_seen: number
  count: number
}

// ---- 编队（当前本丸共用档案 /api/data/honmaru-profile）----

export interface FormationCandidate {
  observation_id: string
  row_no: number | null
  sword_catalog_id: string | null
  same_team_exclusion_key: string | null
  name_zh: string | null
  level: number | null
  tou_level: number | null
  survival: number | null
  survival_max: number | null
  fatigue: number | null
  fatigue_max: number | null
  stats: Record<string, number>
  /** 历史字段名，实际是「显现日期」（获得日期），每振都有，不是极化证据 */
  kiwame_date: string | null
  /** 形态结论：kiwame=极 / normal=普通 / ambiguous=有极化记录但分不清哪振 / unknown=未确认 */
  form_status?: 'kiwame' | 'normal' | 'ambiguous' | 'unknown'
  form_evidence?: string[]
  locked: boolean | null
  page_no: number | null
  unknown_fields: string[]
  observed_at: number | null
  source_snapshot_id: number | null
}

export interface FormationCandidatePool {
  done: boolean
  reason?: string
  completeness?: string
  observed_at: number | null
  owned?: number | null
  entry_count?: number
  entries: FormationCandidate[]
  skipped_newer_snapshots?: Array<{ snapshot_id: number; captured_at: number | null; source: string; completeness: string }>
}

export interface FormationSlot {
  slot: number | null
  slot_status: string
  link_status: string
  observation_id: string | null
  same_team_exclusion_key: string | null
  candidate_ids: string[]
  match_basis: string
  link_reason: string | null
  observed: {
    name?: string | null
    level?: number | null
    kiwame_status?: string | null
    slot_status?: string
    [key: string]: unknown
  }
}

export interface FormationTeam {
  team_no: number
  observation_status: string
  observed_at: number | null
  source_event_id: number | null
  slots: FormationSlot[]
}

export interface HonmaruFormationProfile {
  schema_version: number
  generated_at: number
  done?: boolean
  error?: string
  candidate_pool: FormationCandidatePool
  roster: { teams: FormationTeam[] }
}

// ---- 预设编队 /api/custom-formations ----

/** 预设里一个槽位存的刀剑档案快照；槽位可缺省（=应用时该位置不动） */
export interface CustomFormationSlotEntry {
  selection_policy?: 'locked_highest_level'
  observation_id?: string
  sword_catalog_id?: string
  same_team_exclusion_key?: string
  name_zh?: string
  level?: number
  tou_level?: number
  survival_max?: number
  stats?: Record<string, number | null>
  form_status?: string
  kiwame_date?: string
  source_snapshot_id?: number
  observed_at?: number
  treasure?: { name: string; level: number; affection: number }
  troops?: Record<string, string>
  horse?: string
  charm?: string
}

export interface CustomFormation {
  id: string
  name: string
  target_team: number
  slots: Record<string, CustomFormationSlotEntry>
  created_at: string
  updated_at: string
}

/** 新建/编辑预设时递交给后端的正文（id 走后端生成/路径参数，不在 body 里） */
export type CustomFormationDraft = Pick<CustomFormation, 'name' | 'target_team' | 'slots'>

// ---- 刀帐档案（/api/data/sword-archive）----

export type SwordFormStatus = 'kiwame' | 'normal' | 'ambiguous' | 'unknown'

export interface SwordArchiveHuman {
  id: number
  form: 'kiwame' | 'normal' | null
  keeper: boolean
  /** 常用：顺手就要用的刀 */
  favorite: boolean
  /** 特别关心：置顶盯着的刀（组件里注意别和 vue 的 watch API 撞名解构） */
  watch: boolean
  note: string | null
  /** 人工确认的等级（只补机器读不出的空缺）；没有为 null */
  level: number | null
  confirmed_at: number
  stale: boolean
}

/** 机器独立形态结论（不受人工改判影响，撤销回退就靠它） */
export type SwordMachineFormStatus = SwordFormStatus | null

export interface SwordArchiveEntry {
  acquisition?: { label: string; location?: string; mailbox_id?: string; origin_message?: string; inbox_at?: string; received_at?: string } | null
  observation_id: string
  serial_id?: number | null
  data_source?: 'youzu_log' | 'ocr'
  observed_at?: number | null
  survival?: number | null
  survival_max?: number | null
  fatigue?: number | null
  locked?: boolean | null
  stats?: Record<string, number | null>
  sword_catalog_id: string | null
  name_zh: string | null
  sword_type: string | null
  level: number | null
  tou_level: number | null
  /** 累积经验（training 快照通道扩容后才有；旧档案行没有这些字段，后端给 null 不编 0） */
  exp?: number | null
  /** 乱舞累积习合值；同上，缺了给 null */
  ranbu_exp?: number | null
  /** 内番已喂数值（生存/打击/统率/机动/冲力/侦察/隐蔽，原始刀池行字段）；旧行 null */
  hp_up?: number | null
  atk_up?: number | null
  def_up?: number | null
  mobile_up?: number | null
  back_up?: number | null
  scout_up?: number | null
  hide_up?: number | null
  /** 历史字段名，实为「显现日期」（获得日期），展示必须叫显现日期 */
  kiwame_date: string | null
  form_status: SwordFormStatus
  /** 机器自己的形态结论；没人工的行展示「盘点识别」，改判行拿它当「原识别」 */
  machine_form_status: SwordMachineFormStatus
  /** 人工结论和机器结论冲突（你改判了），徽标从「你确认过」升级成「你改判的」 */
  form_overridden: boolean
  form_evidence: string[]
  unknown_fields: string[]
  human: SwordArchiveHuman | null
  hints: string[]
}

export type SwordAttentionReason = 'form_unknown' | 'form_ambiguous' | 'duplicate_fingerprint' | 'stale_annotation' | 'level_unknown'

export interface SwordArchiveAttentionItem {
  serial_id?: number | null
  departure_reason?: '链结' | '习合' | '刀解' | null
  annotation_id?: number
  observation_id: string | null
  sword_catalog_id: string | null
  name_zh: string | null
  level: number | null
  kiwame_date: string | null
  reasons: SwordAttentionReason[]
  hints: string[]
}

export interface SwordArchiveSummary {
  total: number
  human_confirmed: number
  keepers: number
  attention_count: number
}

export interface SwordArchiveResponse {
  done: boolean
  data_source?: 'youzu_log' | 'ocr'
  reason: string | null
  observed_at: number | null
  snapshot_id: number | null
  summary: SwordArchiveSummary
  entries: SwordArchiveEntry[]
  attention: SwordArchiveAttentionItem[]
  historical_annotations?: SwordArchiveAttentionItem[]
  sword_departures?: Array<{ serial_id: number; ts: number; reason: string; name: string | null }>
}

export interface SwordAnnotationBody {
  serial_id?: number | null
  sword_catalog_id: string | null
  kiwame_date: string | null
  level_at_mark?: number
  level_confirmed?: number | null
  form_confirmed?: 'kiwame' | 'normal' | null
  keeper?: boolean | null
  favorite?: boolean | null
  watch?: boolean | null
  note?: string | null
}

export interface SwordArchiveAnnotation {
  id: number
  sword_catalog_id: string
  [key: string]: unknown
}

// ---- 练度档案（/api/data/training/overview · /api/data/training/history/{serial_id}）----

/** 乱舞下一级差额；满级/超国服未知上限/稀有度查不出时为 null */
export interface RanbuNext {
  /** 到下一级还差的累计习合值 */
  need_exp: number
  /** 估算还需同名刀振数：need_exp / 100 向上取整（100 习合值/振是国服公告口径，未实测，别当实测值展示） */
  need_swords_est: number
}

export interface TrainingOverviewSword {
  serial_id: number
  sword_id: number | null
  /** 刀名：sword_db + youzu_log._sword_name 解析（极化带「·极」）；查不到退「刀帐{id}」 */
  name: string | null
  level: number | null
  exp: number | null
  ranbu_level: number | null
  ranbu_exp: number | null
  ranbu_next: RanbuNext | null
  captured_at: string
}

export interface TrainingOverviewResponse {
  roster_complete?: boolean
  ts: number
  captured_at: string
  sword_count: number
  swords: TrainingOverviewSword[]
}

export interface TrainingHistoryPoint {
  captured_at: string
  ts: number
  level: number | null
  exp: number | null
  ranbu_level: number | null
  ranbu_exp: number | null
}

export interface TrainingHistoryResponse {
  serial_id: number
  timeline: TrainingHistoryPoint[]
  /** 链上第一次观测到 level=99 的 captured_at；从没观测到为 null。注意是「首次观测到」不是「首次达成」 */
  first_max_level_observed_at: string | null
}

// ---- 掉落统计（/api/data/drop-stats?days=N，days 缺省 30，0=全部）----

export type DropStatsGroupKind = 'map' | 'raid' | 'osaka'

export interface DropStatsDrop {
  /** 刀名认不出为 null；count 是掉落收据条数，first_get_count 是其中首次入手的 */
  name: string | null
  count: number
  first_get_count: number
}

export interface DropStatsGroup {
  /** 地图组 = 「章-图」（如 "8-2"）；raid = 固定 "联队战"；osaka = 固定 "大阪城" */
  key: string
  kind: DropStatsGroupKind
  /** 分母（总战斗数）：手动 battle.completed 逐场 + 面板圈（battle_count，读不出的 completed 圈记 1）+ 联队战 battle_taps + 大阪城每层记 1；not_observed 圈已剔除 */
  battles: number
  /** 王点到达数：地图组 = 面板圈 outcome=completed 数；纯手动来源的组（含联队战/大阪城）为 null——手动侧 square_id 哪格是王点未校准，绝不猜 */
  boss_reached: number | null
  drops: DropStatsDrop[]
  drop_total: number
}

export interface DropStatsResponse {
  schema_version: number
  generated_at: number
  window: { days: number; from_ts: number | null; to_ts: number }
  groups: DropStatsGroup[]
  /** 进不了任何分组的诚实记账：缺 chapter/map_no 的 battle.completed 与 sortie.drop/battle.drop 收据落这里，账能对上 */
  unattributed: { battles: number; drop_total: number }
}

// ---- 内番养成视图（/api/data/training/internal-affairs）----

export interface InternalAffairsSword {
  serial_id: number
  sword_id: number | null
  name: string | null
  /** 内番已喂数值：生存（hp_up）/侦察（scout_up），只有这两个是内番专属；其他 *_up 炼结也能喂，不出 */
  hp_up: number | null
  scout_up: number | null
  /** 疑似喂满：自己快照链上最近连续 plateau_k 条不增长。平台期启发式——内番上限表未校准，true 是「连续多次收账没再涨」，可能喂满也可能没喂；快照不足 plateau_k 条为 null（判定不出） */
  hp_plateau: boolean | null
  scout_plateau: boolean | null
  captured_at: string
}

export interface InternalAffairsResponse {
  ts: number
  captured_at: string
  sword_count: number
  /** plateau 判定窗口 K：连续 K 条快照不增长才算疑似喂满 */
  plateau_k: number
  swords: InternalAffairsSword[]
}

// ---- 锻刀串链（/api/data/forge-history?days=N，days 缺省 30，0=全部）----

export interface ForgeHistoryRecipe {
  /** 配方字段；面板锻刀流落的 started 没有配方，全 null */
  charcoal: number | null
  steel: number | null
  coolant: number | null
  file: number | null
}

export interface ForgeHistorySecretary {
  sword_id: number | null
  name: string | null
  /** 近侍观测时刻（登录时刻）：局内换人无法分辨，这是开炉前最近一次登录观测 */
  observed_at: number
}

export interface ForgeHistorySword {
  name: string | null
  sword_id: number | null
  serial_id: number | null
  /** 面板锻刀流认出的单刀没有初入手标记 → null */
  is_first_get_sword: boolean | null
}

export interface ForgeHistoryForge {
  slot_no: number
  started_at: number
  recipe: ForgeHistoryRecipe
  /** 开炉时刻之前最近一条 secretary.observed；一条都没有 → null */
  secretary: ForgeHistorySecretary | null
  /** 领取时刻；炉还没收 → null */
  collected_at: number | null
  /** 领取结果；没收到 → null */
  swords: ForgeHistorySword[] | null
  /** 配方消耗估计：count>1 按配方×count，普通×1；配方全缺 → null。不含委托符/加速符（在 resource.change） */
  cost_est: ForgeHistoryRecipe | null
}

export interface ForgeHistoryOrphanCollected {
  slot_no: number
  collected_at: number
  count: number
  swords: ForgeHistorySword[] | null
}

export interface ForgeHistoryResponse {
  schema_version: number
  generated_at: number
  window: { days: number; from_ts: number | null; to_ts: number }
  /** 有 started 的炉（含没收到领取的），started_at 升序 */
  forges: ForgeHistoryForge[]
  /** 配不上的领取（开炉在采集起点之前/窗外）：老实列出，不硬撮合 */
  orphan_collected: ForgeHistoryOrphanCollected[]
}

// ---- 活动点数历史（/api/data/event-points，不给 event_id 返回活动列表）----

export interface EventPointsActivity {
  event_id: string
  type: number
  /** 游戏服务器时间原文（国服 +08:00），原样给出 */
  start_at: string
  end_at: string
}

export interface EventPointsListResponse {
  schema_version: number
  generated_at: number
  /** 这条日历是哪天观测到的（登录同步）；从没同步过 → null */
  observed_at: number | null
  /** 最新一条 activity.calendar 的 events；event_id→活动名本地没有，原样给 id，名字由前端活动知识解析 */
  events: EventPointsActivity[]
}

export interface EventPointsPoint {
  ts: number
  /** 截图/同步时刻的游戏时间原文；payload 没带 → null */
  captured_at: string | null
  points: number
}

export interface EventPointsTimelineResponse {
  schema_version: number
  generated_at: number
  event_id: string
  type: number | null
  start_at: string
  end_at: string
  /** 切片窗：[start_at, end_at+grace]；活动结束后 1 天内的读数仍算收尾读数，start 前没有宽限 */
  window: { from_ts: number; to_ts: number; grace_seconds: number }
  /** 升序；读数读不出的帧跳过（不补 0），前端画断点 */
  timeline: EventPointsPoint[]
}

// ---- 入手履历（/api/data/sword-journal/{serial_id}）----

export type SwordJournalKind = 'obtained' | 'departed' | 'returned' | 'max_level_observed'

export interface SwordJournalObtainedDetail {
  name: string | null
  sword_id: number | null
  /** 收据来源（forge/sortie.drop/inbox.claim…）；收据缺 → null */
  source: string | null
  chapter: number | null
  map_no: number | null
  is_first_get_sword: boolean | null
  /** 收据原文（数字或时间串）；acquired_at 解析失败时 ts 退化为事件观测时刻 */
  acquired_at: number | string | null
  /** 刀帐档案的首次获得日（游戏原文）；档案缺这一段 → null */
  archive_created_at: string | null
}

export interface SwordJournalEntry {
  /** 事实时刻；档案 created_at 解析不出的入手条目为 null（排最后） */
  ts: number | null
  kind: SwordJournalKind
  detail: SwordJournalObtainedDetail | { finished_at?: string } | { captured_at: string; level: number } | Record<string, never>
}

export interface SwordJournalResponse {
  schema_version: number
  generated_at: number
  serial_id: number
  /** ts 升序；缺哪段就没有哪段，不编。max_level_observed 是「快照链首次观测到 99」不是「首次达成」 */
  timeline: SwordJournalEntry[]
}

// ---- 模板工坊 /api/template-lab（开发专用，打包版不启用）----

export interface TemplateLabStatus {
  enabled: boolean
  ledger_mode: boolean
  adb_ready: boolean
}

export interface TemplateLabFrame {
  idx: number
  name: string
  width: number
  height: number
  mtime?: number
}

export interface TemplateLabCaptureResult {
  session: string
  frames: TemplateLabFrame[]
  errors: string[]
}

export interface TemplateLabSession {
  id: string
  memo?: string | null
  frames: TemplateLabFrame[]
}

export interface TemplateLabDraft {
  name: string
  width: number
  height: number
  mtime?: number
}

export interface TemplateLabCropResult {
  draft: { name: string; width: number; height: number }
}

export interface TemplateLabVerifyRow {
  session: string
  frame: number
  score: number
  loc: { x: number; y: number }
  hit: boolean
}

export interface TemplateLabConfusionRow {
  session: string
  frame: number
  other_draft: string
  other_score: number
  margin: number
}

export interface TemplateLabVerifyResult {
  draft: string
  threshold: number
  results: TemplateLabVerifyRow[]
  confusion: TemplateLabConfusionRow[]
}

export interface TemplateLabAdoptResult {
  ok: boolean
  path: string
  backup: string | null
}

export interface TemplateLabRoi {
  name: string
  x: number
  y: number
  w: number
  h: number
  updated: number
}

export interface TemplateLabOcrRoi {
  name: string | null
  x: number
  y: number
  w: number
  h: number
}

export interface TemplateLabOcrRow {
  session: string
  frame: number
  texts: string[]
}

export interface TemplateLabOcrTestResult {
  roi: TemplateLabOcrRoi
  results: TemplateLabOcrRow[]
}

export type TemplateLabRectXyxy = [number, number, number, number]

export interface TemplateLabCodeRoi {
  id: string
  label: string
  used_in: string
  purpose: string
  default: TemplateLabRectXyxy
  override: TemplateLabRectXyxy | null
  effective: TemplateLabRectXyxy
  overridden: boolean
}

// ---- 流程工坊 /api/flow-lab（开发专用，打包版不启用）----

export type FlowStepCategory = '认' | '点' | '结构'
export type FlowOnFail = 'stop' | 'continue' | 'retry'

export interface FlowStep {
  id: string
  type: string
  label?: string
  params: ScriptParams
  on_fail: FlowOnFail
  retry_times?: number
  retry_interval_s?: number
}

export interface FlowLabFlow {
  id: string
  name: string
  steps: FlowStep[]
  official?: boolean
}

export interface FlowStepDef {
  type: string
  label: string
  desc: string
  category: FlowStepCategory
  params: ParamField[]
}

export interface FlowBuiltinDef {
  name: string
  label: string
  desc: string
  params: ParamField[]
}

export interface FlowTestResult {
  kind: 'recognize' | 'preview'
  action?: 'click' | 'swipe' | 'sleep'
  hit?: boolean
  score?: number | null
  point?: [number, number] | null
  texts?: string[] | null
  from?: [number, number]
  to?: [number, number]
  duration_ms?: number
  seconds?: number
  note?: string
}

// ── 远征排班：班次实况（automation.slot_states 的前端投影）──
export type ExpeditionSlotState =
  | 'pending'         // 还没到点
  | 'waiting_busy'    // 到点但自家任务在跑
  | 'waiting_unknown' // 接管门卫没过 / 游戏没在跑 / 上次派遣没确认
  | 'ready'           // 即将接管（15 秒预告窗口）
  | 'dispatched'      // 已确认派出
  | 'expired'         // 超过最大延迟，明确跳过
  | 'failed_unknown'  // 连续 3 次无法确认结果
  | 'missed'          // 计划时间已过，但那会儿面板没在线

export interface ExpeditionSlotStatus {
  time: string
  team_no: number
  map_code: string
  state: ExpeditionSlotState
  blocked_reason?: string
  next_retry_in_min?: number | null
  late_min?: number
}

export interface ExpeditionToday {
  preset: Array<ExpeditionSlotStatus & { lane: number; offset_min: number }>
  custom: Array<ExpeditionSlotStatus & { index: number }>
}

export interface ExpeditionAutomation {
  enabled: boolean
  mode: 'preset' | 'custom'
  preset: string
  start_time: string
  teams: number[]
  capitalist: boolean
  sakura_before_dispatch?: boolean
  paused_until: string
  /** 一班最多允许晚多少分钟；资本家模式补跑窗口是它的 4 倍封顶 */
  max_delay_min: number
  last_runs?: Record<string, string>
  lane_shifts?: Record<string, number>
  slot_states?: Record<string, unknown>
}

export interface ExpeditionScheduleEntry {
  time: string
  team_no: number
  map_code: string
  map_name?: string
  enabled: boolean
  last_fired?: string
}

export interface ExpeditionSchedule {
  version: number
  common_plan: Array<{ team_no: number; map_code: string; enabled: boolean; formation_id?: string }>
  automation: ExpeditionAutomation
  entries: ExpeditionScheduleEntry[]
  maps: Array<{ code: string; era: number; slot: number; name: string; duration_min: number; duration_text: string }>
  presets: Record<string, { lanes: Array<Array<{ offset_min: number; map_code: string; duration_min: number }>>; totals: string }>
  today?: ExpeditionToday
}

export interface DayTimelineMarker {
  time_min: number
  label: string
  kind: string
}

export interface DayTimelineExpedition {
  sakura_before_dispatch?: boolean
  repair_threshold?: 'light' | 'medium' | 'heavy'
  formation_id?: string
  formation_name?: string
  key: string
  /** forced = 玩家点的班（含建议采纳的自描述班）；running = 远征中/待收 */
  kind: 'forced' | 'running'
  planned_at: number
  time_min: number
  duration_min: number
  team_no: number
  map_code: string
  state: string
  blocked_reason: string
  late_min: number
  enabled: boolean
  base_enabled: boolean
  /** 自定义排班里该条目自身的开关（preset 模式恒 true） */
  entry_enabled: boolean
  skipped_today: boolean
  forced_today: boolean
  /** 今天到底跑不跑：排班开着且未跳过，或被单班强制启用 */
  will_run: boolean
  toggleable: boolean
}

export interface DayTimelineRun {
  script: string
  label: string
  started_at: number
  ended_at: number | null
  status: string
  tone: 'ok' | 'failed' | 'stopped' | 'running' | 'waiting'
}

export interface DayTimelineSuggestion {
  start_min: number
  duration_min: number
  runs?: number
  note: string
}

/** 远征建议引擎的长期偏好：可丢的队伍各派几次 + 哪些队可以丢（换日不重置） */
export interface DayExpeditionHelpPrefs {
  team_formations?: Record<string, string>
  resource_focus?: string
  suggested_resource?: string
  rounds_per_team: number
  available_teams: number[]
}

/** 一条远征建议：引擎按缺口现算的班（队伍/图/时刻自描述）；点采纳 = 记 forced */
export interface DayExpeditionSuggestion {
  formation_signature?: string
  formation_id?: string
  formation_name?: string
  blocked_resource?: string | null
  restrictions?: string[]
  formation_change?: boolean
  kind: 'expedition'
  key: string
  team_no: number
  map_code: string
  map_name: string
  resource: string
  duration_min: number
  start_min: number
  shift_no: number
  reason: string
}

export type ScheduleBlockKind = 'raid' | 'activity' | 'workflow' | 'daily'

/** 计划块 v2：activity 带 script/event_key/runs，raid 保留旧安排，workflow 带 workflow_id */
export interface DayScheduleBlock {
  steps?: WorkflowProjectionStep[]
  start_min: number
  kind: ScheduleBlockKind
  runs?: number
  workflow_id?: string
  script?: string
  event_key?: string
  after_raids?: boolean
}

export interface DayBooking {
  version: number
  day_start: number
  /** 没有联队战活动时允许为 null（纯 workflow/daily 安排） */
  event_end_at: number | null
  blocks: DayScheduleBlock[]
  issues: string[]
}

export type ConductorBlockStatus =
  | 'pending'
  | 'running'
  | 'ended'
  | 'interrupted'
  | 'missed'
  | 'blocked'

/** 大总管块：计划块 + 展示名 + 运行状态 */
export interface DayConductorBlock extends DayScheduleBlock {
  recovery?: { completed: number; remaining: number; uncertain_round: boolean }
  waiting_until?: number
  label: string
  status: ConductorBlockStatus
  run_id?: string
  started_at?: number
  finished_at?: number
  reason?: string
}

export interface DayConductor {
  enabled: boolean
  available: boolean
  workflow_id: string
  workflow_name: string
  blocks: DayConductorBlock[]
  issues: string[]
  options: Array<{ id: string; name: string }>
}

export interface ScheduledGameplayOption {
  script: string
  label: string
  available: boolean
  event_key: string
  end_at: number | null
}

export interface DayTimeline {
  workflow_active?: { name: string; steps: WorkflowProjectionStep[] } | null
  workflow_waits?: Array<{ id: string; name: string; wake_at: number; status: string; reason?: string; steps: WorkflowProjectionStep[] }>
  gameplay_options?: ScheduledGameplayOption[]
  now: number
  day_start: number
  markers: DayTimelineMarker[]
  expeditions: DayTimelineExpedition[]
  expedition_schedule_enabled: boolean
  expedition_help: DayExpeditionHelpPrefs
  expedition_suggestions: DayExpeditionSuggestion[]
  /** 建议为空时给玩家看的原因（没盘点/今天不丢队/排不下） */
  expedition_advice_note: string | null
  runs: DayTimelineRun[]
  hint: string | null
  suggestions: DayTimelineSuggestion[] | null
  shortfall_seconds: number | null
  booking: DayBooking | null
  conductor: DayConductor
  activity?: {
    name: string
    target_runs: number
    planned_runs: number
    completed_today: number
    seconds_per_loop: number
    remaining_runs: number
    event_end_at: number
    occupied: { start_min: number; end_min: number; label: string }[]
  } | null
}
