import type { TrainingOverviewResponse, TrainingHistoryResponse, InternalAffairsResponse, DropStatsResponse, ForgeHistoryResponse, EventPointsListResponse, EventPointsTimelineResponse, SwordJournalResponse } from './types'
import type { EventGoalResult, EventTimelineReport, EventsCalendar, FlowBuiltinDef, FlowLabFlow, FlowStep, FlowStepDef, FlowTestResult, HomeLayout, HomeLayoutEntry, Incident, LedgerImportPreview, LedgerOnboarding, ManualInventory, ManualSession, PlanningReport, ResourceLedger, ScriptInfo, ScriptParams, ScriptsResponse, SwordAnnotationBody, SwordArchiveAnnotation, SwordArchiveResponse, SwordInventoryResponse, TemplateLabAdoptResult, TemplateLabCaptureResult, TemplateLabCodeRoi, TemplateLabCropResult, TemplateLabDraft, TemplateLabOcrTestResult, TemplateLabRectXyxy, TemplateLabRoi, TemplateLabSession, TemplateLabStatus, TemplateLabVerifyResult, WorkflowNodeDef, WorkflowPreset } from './types'
import type { DailyReport, JpNetlogImportResult } from './types'

import type { HonmaruHomeData, HonmaruProfile, HonmaruNote, HonmaruSituation, WorkflowIdentity } from './types'
import type { CustomFormation, CustomFormationDraft, HonmaruFormationProfile } from './types'
import type { ExpeditionSchedule, DayTimeline, DayBooking, DayScheduleBlock } from './types'

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const body = await response.json()
  if (!response.ok) throw new Error(body.detail || body.error || body.reason || `请求失败（${response.status}）`)
  return body as T
}

async function optionalRecord<T>(url: string): Promise<T | null> {
  const response = await fetch(url)
  const body = await response.json()
  if (response.status === 404 && body.detail !== 'Not Found') return null
  if (!response.ok) throw new Error(body.detail || `请求失败（${response.status}）`)
  return body as T
}

export const api = {
  trainingOverview: (server = '') => optionalRecord<TrainingOverviewResponse>(`/api/data/training/overview${server ? `?server=${server}` : ''}`),
  trainingHistory: (id: number, server = '') => optionalRecord<TrainingHistoryResponse>(`/api/data/training/history/${id}${server ? `?server=${server}` : ''}`),
  internalAffairs: (server = '') => optionalRecord<InternalAffairsResponse>(`/api/data/training/internal-affairs${server ? `?server=${server}` : ''}`),
  dropStats: (days: number) => request<DropStatsResponse>(`/api/data/drop-stats?days=${days}`),
  forgeHistory: (days: number) => request<ForgeHistoryResponse>(`/api/data/forge-history?days=${days}`),
  eventPointsList: () => request<EventPointsListResponse>('/api/data/event-points'),
  eventPoints: (id: string) => request<EventPointsTimelineResponse>(`/api/data/event-points?event_id=${encodeURIComponent(id)}`),
  swordJournal: (id: number) => request<SwordJournalResponse>(`/api/data/sword-journal/${id}`),
  honmaruHome: () => request<HonmaruHomeData>('/api/honmaru-home'),
  honmaruSituation: () => request<{ situation: HonmaruSituation | null }>('/api/honmaru-home/situation'),
  refreshHonmaruSituation: () => request<{ situation: HonmaruSituation }>('/api/honmaru-home/situation/refresh', { method: 'POST' }),
  saveHonmaruProfile: (profile: HonmaruProfile) => request<{ profile: HonmaruProfile }>('/api/honmaru-home/profile', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(profile),
  }),
  saveHonmaruNote: (body: string, id?: string) => request<{ note: HonmaruNote }>(`/api/honmaru-home/notes${id ? `/${encodeURIComponent(id)}` : ''}`, {
    method: id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ body }),
  }),
  appMode: () => request<{ mode: 'automation' | 'ledger'; automation_enabled: boolean }>('/api/app-mode'),
  scripts: () => request<ScriptsResponse>('/api/scripts'),
  gameplaySettings: (script: string) => request<{ info: ScriptInfo; params: ScriptParams }>(`/api/gameplay-settings/${encodeURIComponent(script)}`),
  saveGameplaySettings: (script: string, params: ScriptParams) => request<{ params: ScriptParams }>(`/api/gameplay-settings/${encodeURIComponent(script)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ params }),
  }),
  dayTimeline: () => request<DayTimeline>('/api/day-timeline'),
  /** 切今天这一班跑不跑：willRun=true 时排班关着会记 forced 单独跑 */
  setDayExpeditionSlot: (key: string, willRun: boolean) => request<{ ok: boolean }>('/api/day-timeline/expedition-slot', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key, will_run: willRun }),
  }),
  /** 采纳一条远征建议：那班记 forced（排班关着也单独跑），绝不自动执行 */
  expeditionSettings: (code: string) => request<{ rewards: Record<string, number>; formations: Record<string, Array<{formation_id: string; formation_name: string; formation_signature: string}>> }>(`/api/day-timeline/expedition-settings/${encodeURIComponent(code)}`),
  configureDayExpedition: (body: Record<string, unknown>) => request<{ok: boolean}>('/api/day-timeline/expedition-adopt', { method: 'PUT', body: JSON.stringify(body) }),
  adoptDayExpeditionSuggestion: (teamNo: number, mapCode: string, startMin: number, formationId?: string, formationSignature?: string) => request<{ ok: boolean; expeditions: DayTimeline['expeditions']; expedition_suggestions: DayTimeline['expedition_suggestions']; expedition_help: DayTimeline['expedition_help']; expedition_advice_note: string | null }>('/api/day-timeline/expedition-adopt', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ team_no: teamNo, map_code: mapCode, start_min: startMin, formation_id: formationId, formation_signature: formationSignature }),
  }),
  /** 记住远征建议偏好：可丢的队伍各派几次 + 哪些队可以丢（长期，换日不重置） */
  setExpeditionHelpPrefs: (roundsPerTeam: number, availableTeams: number[]) => request<{ ok: boolean; expedition_help: DayTimeline['expedition_help'] }>('/api/expedition-help-prefs', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rounds_per_team: roundsPerTeam, available_teams: availableTeams }),
  }),
  setExpeditionTeamFormations: (teamFormations: Record<string, string>) => request('/api/expedition-help-prefs', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ team_formations: teamFormations }),
  }),
  setExpeditionResourceFocus: (resourceFocus: string) => request('/api/expedition-help-prefs', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ resource_focus: resourceFocus }),
  }),
  saveDayRaidPlan: (blocks: DayScheduleBlock[]) => request<{ booking: DayBooking }>('/api/day-timeline/raid-plan', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ blocks }),
  }),
  /** 保存今日时段表并当场开启大总管：保存即 armed */
  saveDaySchedule: (blocks: DayScheduleBlock[], raidWorkflowId?: string) => request<{ conductor: DayTimeline['conductor']; booking: DayTimeline['booking'] }>('/api/day-timeline/schedule', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ blocks, raid_workflow_id: raidWorkflowId }),
  }),
  executeToday: () => request<{ ok: boolean; message: string }>('/api/today/execute', { method: 'POST' }),
  resumeDayRaid: (runId: string, finishedRound: boolean) => request<{ ok: boolean }>('/api/day-conductor/resume-raid', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ run_id: runId, finished_round: finishedRound }),
  }),
  setDayConductor: (enabled: boolean, workflowId?: string) => request<{ conductor: DayTimeline['conductor'] }>('/api/day-conductor', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled, workflow_id: workflowId }),
  }),
  settings: () => request<{ params?: Record<string, ScriptParams>; theme?: string; backdrop?: string; scenery?: string; companion?: string }>('/api/saved-settings'),
  saveCompanion: (companion: string) => request<{ ok: boolean }>('/api/saved-settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ companion }),
  }),
  saveScenery: (scenery: string) => request<{ ok: boolean }>('/api/saved-settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scenery }),
  }),
  saveSettings: (params: Record<string, ScriptParams>) => request<{ ok: boolean }>('/api/saved-settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ params }),
  }),
  saveTheme: (theme: string) => request<{ ok: boolean }>('/api/saved-settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ theme }),
  }),
  saveBackdrop: (backdrop: string) => request<{ ok: boolean }>('/api/saved-settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ backdrop }),
  }),
  run: (script: string, params: ScriptParams) => request<{ ok: boolean; run_id?: string; workflow?: WorkflowIdentity | null }>('/api/scripts/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ script, params }),
  }),
  honmaruProfile: () => request<HonmaruFormationProfile>('/api/data/honmaru-profile'),
  clientInventory: () => request<import('./types').ClientInventory>('/api/data/client-inventory'),
  customFormations: () => request<{ formations: CustomFormation[] }>('/api/custom-formations'),
  saveCustomFormation: (record: CustomFormationDraft, id?: string) => request<{ ok: boolean; formation: CustomFormation }>(`/api/custom-formations${id ? `/${encodeURIComponent(id)}` : ''}`, {
    method: id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(record),
  }),
  deleteCustomFormation: (id: string) => request<{ ok: boolean }>(`/api/custom-formations/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  stop: () => request<{ ok: boolean }>('/api/scripts/stop', { method: 'POST' }),
  workflows: () => request<{ presets: WorkflowPreset[] }>('/api/workflows'),
  cancelWorkflowWait: (id: string) => request<{ ok: boolean }>(`/api/workflows/waits/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  homeLayout: () => request<HomeLayout>('/api/home-layout'),
  saveHomeLayout: (order: string[], hidden: string[]) => request<{ ok: boolean; entries: HomeLayoutEntry[] }>('/api/home-layout', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ order, hidden }),
  }),
  workflowNodes: () => request<{ nodes: WorkflowNodeDef[] }>('/api/workflows/nodes'),
  createWorkflow: (preset: Omit<WorkflowPreset, 'id'>) => request<{ ok: boolean; id?: string }>('/api/workflows', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(preset),
  }),
  updateWorkflow: (preset: WorkflowPreset) => request<{ ok: boolean }>(`/api/workflows/${encodeURIComponent(preset.id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(preset),
  }),
  deleteWorkflow: (id: string) => request<{ ok: boolean }>(`/api/workflows/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  dashboard: () => request<any>('/api/dashboard'),
  dataSummary: (days = 30) => request<any>(`/api/data/summary?days=${days}`),
  swordInventory: () => request<SwordInventoryResponse>('/api/data/sword-inventory/latest'),
  swordArchive: () => request<SwordArchiveResponse>('/api/data/sword-archive'),
  saveSwordAnnotation: (body: SwordAnnotationBody) => request<{ ok: boolean; annotation: SwordArchiveAnnotation }>('/api/data/sword-archive/annotations', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }),
  revokeSwordAnnotation: (id: number) => request<{ ok: boolean }>(`/api/data/sword-archive/annotations/${id}`, { method: 'DELETE' }),
  resourceLedger: (days = 7, server = '') => request<ResourceLedger>(`/api/data/resource-ledger?days=${days}${server ? `&server=${server}` : ''}`),
  resourceLedgerRange: (from: number, to: number) => request<ResourceLedger>(`/api/data/resource-ledger?from=${from}&to=${to}`),
  dailyReport: (date = '', server = '') => {
    const params = new URLSearchParams()
    if (date) params.set('date', date)
    if (server) params.set('server', server)
    const qs = params.toString()
    return request<DailyReport>(`/api/daily_report${qs ? `?${qs}` : ''}`)
  },
  ledgerOnboarding: () => request<LedgerOnboarding>('/api/data/ledger-onboarding'),
  gameInventoryResult: () => request<{ result: { run_id: string; payload: { records: string; ocr: string; resources: Record<string, number> } } | null }>('/api/data/game-inventory'),
  updateLedgerOnboarding: (action: 'start' | 'advance' | 'complete' | 'dismiss', step?: 2 | 3) => request<LedgerOnboarding & { ok: boolean }>('/api/data/ledger-onboarding', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, step }),
  }),
  ledgerExport: async (format: 'xlsx' | 'csv') => {
    const response = await fetch(`/api/data/ledger-export?format=${format}`)
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      throw new Error(body.detail || body.error || body.reason || `导出失败（${response.status}）`)
    }
    const disposition = response.headers.get('Content-Disposition') || ''
    const matched = disposition.match(/filename\*=UTF-8''([^;]+)/i)
    return { blob: await response.blob(), filename: matched ? decodeURIComponent(matched[1]) : `maamaru-ledger.${format}` }
  },
  previewLedgerImport: (file: File) => request<LedgerImportPreview>(`/api/data/ledger-import/preview?filename=${encodeURIComponent(file.name)}`, {
    method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
  }),
  applyLedgerImport: (previewId: string, acceptConflicts: boolean) => request<{ ok: boolean; imported: number; duplicates: number; conflicts: number; backup: string | null }>('/api/data/ledger-import/apply', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ preview_id: previewId, accept_conflicts: acceptConflicts }),
  }),
  jpNetlogImport: (file: File) => request<JpNetlogImportResult>(`/api/data/jp-netlog-import?filename=${encodeURIComponent(file.name)}`, {
    method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
  }),
  manualInventory: (limit = 200) => request<{ schema_version: number; items: ManualInventory[] }>(`/api/data/manual-inventory?limit=${limit}`),
  addManualInventory: (resources: Record<string, number>, observedAt?: number) => request<{ ok: boolean; snapshot: ManualInventory }>('/api/data/manual-inventory', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ resources, observed_at: observedAt }),
  }),
  updateManualInventory: (id: number, resources: Record<string, number>, observedAt: number) => request<{ ok: boolean; snapshot: ManualInventory }>(`/api/data/manual-inventory/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ resources, observed_at: observedAt }),
  }),
  deleteManualInventory: (id: number) => request<{ ok: boolean }>(`/api/data/manual-inventory/${id}`, { method: 'DELETE' }),
  manualSessions: (limit = 200, fromTs?: number, toTs?: number) => request<{ schema_version: number; items: ManualSession[] }>(`/api/data/manual-sessions?limit=${limit}${fromTs == null ? '' : `&from_ts=${fromTs}`}${toTs == null ? '' : `&to_ts=${toTs}`}`),
  addManualSession: (value: { script: string; started_at: number; ended_at: number; loops: number; note?: string }) => request<{ ok: boolean; item: ManualSession }>('/api/data/manual-sessions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  updateManualSession: (id: number, value: { script: string; started_at: number; ended_at: number; loops: number; note?: string }) => request<{ ok: boolean; item: ManualSession }>(`/api/data/manual-sessions/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  deleteManualSession: (id: number) => request<{ ok: boolean }>(`/api/data/manual-sessions/${id}`, { method: 'DELETE' }),
  dataEvents: (limit = 100, beforeId?: number, fromTs?: number, toTs?: number) => request<{ schema_version: number; items: any[]; has_more: boolean; next_cursor: number | null }>(`/api/data/events?limit=${limit}${beforeId == null ? '' : `&before_id=${beforeId}`}${fromTs == null ? '' : `&from_ts=${fromTs}`}${toTs == null ? '' : `&to_ts=${toTs}`}`),
  dataRuns: (limit = 20, beforeStartedAt?: number, fromTs?: number, toTs?: number, status?: string) => request<{ schema_version: number; items: any[]; has_more: boolean; next_cursor: number | null }>(`/api/data/runs?limit=${limit}${beforeStartedAt == null ? '' : `&before_started_at=${beforeStartedAt}`}${fromTs == null ? '' : `&from_ts=${fromTs}`}${toTs == null ? '' : `&to_ts=${toTs}`}${status ? `&status=${encodeURIComponent(status)}` : ''}`),
  attachRunInventory: (runId: string) => request<{ ok: boolean; run: any }>(`/api/data/runs/${encodeURIComponent(runId)}/attach-inventory`, { method: 'POST' }),
  deleteRun: (runId: string) => request<{ ok: boolean }>(`/api/data/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' }),
  humanReports: () => request<{ schema_version: number; items: any[]; inventory_gaps: any[] }>('/api/data/human-reports?limit=500'),
  addHumanReport: (value: any) => request<{ ok: boolean; item: any }>('/api/data/human-reports', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  addHumanReportBatch: (value: any) => request<{ ok: boolean; items: any[]; group_id: string }>('/api/data/human-reports/batch', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  updateHumanReport: (id: number, value: any) => request<{ ok: boolean; item: any }>(`/api/data/human-reports/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  updateHumanReportGroup: (groupId: string, value: any) => request<{ ok: boolean; items: any[]; group_id: string }>(`/api/data/human-reports/group/${encodeURIComponent(groupId)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  deleteHumanReport: (id: number) => request<{ ok: boolean }>(`/api/data/human-reports/${id}`, { method: 'DELETE' }),
  deleteHumanReportGroup: (groupId: string) => request<{ ok: boolean }>(`/api/data/human-reports/group/${encodeURIComponent(groupId)}`, { method: 'DELETE' }),
  planning: () => request<PlanningReport>('/api/planning'),
  addPlanningGoal: (value: { resource?: string; goal_mode?: 'amount_target' | 'deadline_target'; kind?: 'fragment'; fragment?: string; target?: number; deadline?: string; note?: string }) => request<{ ok: boolean; goal: any }>('/api/planning/goals', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  deletePlanningGoal: (id: number) => request<{ ok: boolean }>(`/api/planning/goals/${id}`, { method: 'DELETE' }),
  events: () => request<EventsCalendar>('/api/events'),
  eventsTimeline: () => request<EventTimelineReport>('/api/events/timeline'),
  saveEventEstimate: (event: string, keysPerRun: number) => request<{ ok: boolean }>('/api/planning/event-estimate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event, keys_per_run: keysPerRun }),
  }),
  saveEventTarget: (event: string, target: number) => request<{ ok: boolean }>('/api/planning/event-target', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event, target }),
  }),
  addEventGoal: (event: string, target?: number) => request<EventGoalResult>('/api/planning/event-goals', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event, ...(target == null ? {} : { target }) }),
  }),
  addGameplayGoal: (value: Record<string, unknown>) => request<{ ok: boolean; goal: any; estimate: any }>('/api/planning/gameplay-goal', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  logs: (limit = 200) => request<{ logs: any[] }>(`/api/logs?limit=${limit}`),
  logsForRun: (runId: string, limit = 3000) => request<{ logs: any[] }>(`/api/logs?run_id=${encodeURIComponent(runId)}&limit=${limit}`),
  chatHistory: () => request<{ history: Array<{ role: string; content: string; ts: number }> }>('/api/chat/history'),
  chat: (message: string) => request<{ reply: string }>('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  }),
  configLists: () => request<Record<string, string[]>>('/api/config-lists'),
  swords: () => request<{ swords: Array<{ id: string; name: string; name_zh: string; type: string }> }>('/api/swords'),
  saveConfigLists: (value: Record<string, string[]>) => request<{ ok: boolean }>('/api/config-lists', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  emulatorConfig: () => request<{ adb_address: string; default_address: string; adb_path: string }>('/api/emulator-config'),
  saveEmulatorConfig: (value: { adb_address: string }) => request<{ ok: boolean; adb_address: string }>('/api/emulator-config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  expeditionSchedule: () => request<ExpeditionSchedule>('/api/expedition-schedule'),
  saveExpeditionSchedule: (value: any) => request<{ ok: boolean }>('/api/expedition-schedule', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  pauseExpeditions: (minutes: number) => request<{ ok: boolean; paused_until: string }>('/api/expedition-pause', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ minutes }),
  }),
  chatConfig: () => request<any>('/api/chat-config'),
  botConfig: () => request<any>('/api/bot-config'),
  qqStatus: () => request<any>('/api/qq-status'),
  saveChatConfig: (value: any) => request<{ ok: boolean }>('/api/chat-config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  saveBotConfig: (value: any) => request<any>('/api/bot-config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  incidents: () => request<{ items: Incident[]; unread: number }>('/api/incidents'),
  ackIncident: (code: string) => request<{ ok: boolean }>(`/api/incidents/${encodeURIComponent(code)}/ack`, { method: 'POST' }),
  resolveIncident: (code: string) => request<{ ok: boolean }>(`/api/incidents/${encodeURIComponent(code)}/resolve`, { method: 'POST' }),
  templateLabStatus: () => request<TemplateLabStatus>('/api/template-lab/status'),
  templateLabCapture: (count: number, intervalMs: number, memo = '') => request<TemplateLabCaptureResult>('/api/template-lab/capture', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ count, interval_ms: intervalMs, memo }),
  }),
  templateLabSessions: () => request<{ sessions: TemplateLabSession[] }>('/api/template-lab/sessions'),
  templateLabSessionMemo: (session: string, memo: string) => request<{ ok: boolean; memo: string | null }>('/api/template-lab/session-memo', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session, memo }),
  }),
  templateLabFrameUrl: (session: string, idx: number) => `/api/template-lab/frame?session=${encodeURIComponent(session)}&idx=${idx}`,
  templateLabDraftUrl: (name: string) => `/api/template-lab/draft?name=${encodeURIComponent(name)}`,
  templateLabCrop: (value: { session: string; frame: number; x: number; y: number; w: number; h: number; name: string }) => request<TemplateLabCropResult>('/api/template-lab/crop', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  templateLabDrafts: () => request<{ drafts: TemplateLabDraft[] }>('/api/template-lab/drafts'),
  templateLabVerify: (draft: string, sessions: string[], threshold: number) => request<TemplateLabVerifyResult>('/api/template-lab/verify', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ draft, sessions, threshold }),
  }),
  templateLabAdopt: (draft: string, target: string) => request<TemplateLabAdoptResult>('/api/template-lab/adopt', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ draft, target }),
  }),
  templateLabRois: () => request<{ rois: TemplateLabRoi[] }>('/api/template-lab/rois'),
  templateLabSaveRoi: (value: { name: string; x: number; y: number; w: number; h: number }) => request<{ ok: boolean; roi: TemplateLabRoi }>('/api/template-lab/rois', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  templateLabDeleteRoi: (name: string) => request<{ ok: boolean }>(`/api/template-lab/rois/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  templateLabOcrTest: (name: string, sessions: string[]) => request<TemplateLabOcrTestResult>('/api/template-lab/ocr-test', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, sessions }),
  }),
  templateLabOcrTestRect: (rect: TemplateLabRectXyxy, sessions: string[]) => request<TemplateLabOcrTestResult>('/api/template-lab/ocr-test', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rect, sessions }),
  }),
  templateLabCodeRois: () => request<{ rois: TemplateLabCodeRoi[] }>('/api/template-lab/code-rois'),
  templateLabSaveCodeRoi: (id: string, rect: TemplateLabRectXyxy) => request<{ ok: boolean; roi: TemplateLabCodeRoi }>('/api/template-lab/code-rois', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, rect }),
  }),
  templateLabDeleteCodeRoi: (id: string) => request<{ ok: boolean }>(`/api/template-lab/code-rois/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  flowLabFlows: () => request<{ flows: FlowLabFlow[]; warnings: string[] }>('/api/flow-lab/flows'),
  flowLabCreateFlow: (value: { name: string; steps: FlowStep[] }) => request<{ ok: boolean; flow: FlowLabFlow }>('/api/flow-lab/flows', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
  }),
  flowLabUpdateFlow: (flow: FlowLabFlow) => request<{ ok: boolean; flow: FlowLabFlow }>(`/api/flow-lab/flows/${encodeURIComponent(flow.id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: flow.name, steps: flow.steps }),
  }),
  flowLabDeleteFlow: (id: string) => request<{ ok: boolean }>(`/api/flow-lab/flows/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  flowLabDuplicateFlow: (id: string) => request<{ ok: boolean; flow: FlowLabFlow }>(`/api/flow-lab/flows/${encodeURIComponent(id)}/duplicate`, { method: 'POST' }),
  flowLabCopyFlow: (id: string) => request<{ ok: boolean; flow: FlowLabFlow }>(`/api/flow-lab/flows/${encodeURIComponent(id)}/copy`, { method: 'POST' }),
  flowLabSteps: () => request<{ steps: FlowStepDef[]; builtins: FlowBuiltinDef[] }>('/api/flow-lab/steps'),
  flowLabTemplates: () => request<{ templates: string[] }>('/api/flow-lab/templates'),
  flowLabTemplateImageUrl: (path: string) => `/api/flow-lab/template-image?path=${encodeURIComponent(path)}`,
  flowLabRois: () => request<{ rois: TemplateLabRoi[] }>('/api/flow-lab/rois'),
  flowLabTestStep: (step: FlowStep) => request<FlowTestResult>('/api/flow-lab/test-step', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ step }),
  }),
}
