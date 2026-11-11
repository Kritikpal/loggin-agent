// Types mirror app/schemas/ai.py and app/schemas/campaign.py. Decimals arrive as strings.

export type WorkflowStatus = 'COMPLETED' | 'WAITING_APPROVAL' | 'EXECUTED' | 'REJECTED' | 'INVALID_ACTION' | 'BLOCKED'

export interface ToolCall {
  order: number
  name: string
  args: Record<string, unknown>
  status: string
  result: string | null
}

export interface ProposedAction {
  action_type: 'CREATE_CAMPAIGN'
  name: string
  message: string
  category: string
  discount_percent: string
}

export interface Campaign {
  id: string
  name: string
  message: string
  category: string | null
  discount_percent: string
  status: string
  created_at: string
}

export interface MemoryConflict {
  id: string
  content: string
  evidence: string
  earlier_id: string | null
  earlier_content: string | null
  created_at: string
}

export interface ChatResponse {
  response: string
  tool_calls: ToolCall[]
  status: WorkflowStatus
  thread_id: string | null
  proposed_action: ProposedAction | null
  campaign: Campaign | null
  conversation_id: string | null
  memory_conflicts: MemoryConflict[]
}

export class ApiError extends Error {
  status: number
  code: string
  details?: unknown

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message)
    this.status = status
    this.code = code
    this.details = details
  }
}

interface RequestOptions {
  body?: unknown // sent as JSON
  rawText?: Blob // sent as is (a log file); wins over body
}

async function request<T>(method: 'GET' | 'POST' | 'PATCH' | 'DELETE', path: string, opts: RequestOptions = {}): Promise<T> {
  const { body, rawText } = opts
  const headers: Record<string, string> = {}
  if (rawText === undefined && body !== undefined) headers['Content-Type'] = 'application/json'
  if (rawText !== undefined) headers['Content-Type'] = 'text/plain'
  let res: Response
  try {
    res = await fetch(`/api/v1${path}`, {
      method,
      headers,
      body: rawText ?? (body === undefined ? undefined : JSON.stringify(body)),
    })
  } catch {
    throw new ApiError(0, 'network', 'Could not reach the server. Check your connection and try again.')
  }
  if (res.status === 204) return undefined as T
  const data = await res.json().catch(() => null)
  if (res.ok && data) return data as T
  const err = data?.error // backend envelope: {"error": {code, message, details}}
  if (err) throw new ApiError(res.status, err.code, err.message, err.details)
  throw new ApiError(res.status, 'unreachable', `The API is not responding (HTTP ${res.status}). Is the backend running on port 8000?`)
}

/** A project stored in the database (app/schemas/projects.py). An invalid one cannot be chatted with; `error` says why. */
export interface ProjectSummary {
  id: string
  title: string
  type: string
  status: 'ok' | 'invalid'
  file_count: number
  indexed_from: string | null
  indexed_to: string | null
  error: string | null
}

export const listProjects = () => request<ProjectSummary[]>('GET', '/projects')

export const sendChat = (projectId: string, message: string, conversationId: string | null) =>
  request<ChatResponse>('POST', `/projects/${encodeURIComponent(projectId)}/chat`, { body: { message, conversation_id: conversationId } })

export const resumeWorkflow = (threadId: string, decision: 'APPROVED' | 'REJECTED') =>
  request<ChatResponse>('POST', `/ai/workflows/${threadId}/resume`, { body: { decision } })

export type ConflictChoice = 'USE_NEW' | 'KEEP_EARLIER'

/** Settle a flagged memory: use the correction, or delete it and keep the earlier note. */
export const settleConflict = (memoryId: string, choice: ConflictChoice) =>
  choice === 'USE_NEW'
    ? request<unknown>('POST', `/ai/memories/${memoryId}/accept`)
    : request<unknown>('DELETE', `/ai/memories/${memoryId}`)

export const PROJECT_TYPES = ['wso2', 'spring-boot', 'logback', 'json'] as const

export interface ProjectInput {
  id: string
  title: string
  type: string
  description?: string
  timezone?: string
  event_start?: string
  app_packages?: string[]
  field_mappings?: Record<string, string>
}

/** The reply to creating a project (app/schemas/projects.py ProjectDetail). */
export interface ProjectCreated extends ProjectSummary {
  description: string
  timezone: string
  include: string
}

export interface UploadResult {
  received: number
  stored: number
  duplicates: number
  skipped: number
}

const projectPath = (id: string) => `/projects/${encodeURIComponent(id)}`

export const createProject = (input: ProjectInput) => request<ProjectCreated>('POST', '/projects', { body: input })

export const updateProject = (id: string, changes: Partial<Omit<ProjectInput, 'id'>>) =>
  request<ProjectSummary>('PATCH', projectPath(id), { body: changes })

export const deleteProject = (id: string) => request<void>('DELETE', projectPath(id))

/** Upload a whole log file into a project. */
export const uploadLogs = (id: string, file: Blob) => request<UploadResult>('POST', `${projectPath(id)}/upload`, { rawText: file })

// ---- logs (app/schemas/logs.py) ----

export interface LogItem {
  id: string
  source: string
  occurred_at: string | null
  level: string | null
  text: string
  truncated: boolean
  signature: string | null
  actor: string | null
  action: string | null
}
export interface LogPage {
  items: LogItem[]
  next_cursor: string | null
}
export interface LogDetail {
  id: string
  source: string
  locator: Record<string, unknown>
  occurred_at: string | null
  level: string | null
  raw_text: string
  fields: Record<string, unknown>
  signature: string | null
  signature_template: string | null
}
export interface StatsBucket {
  start: string
  total: number
  by_level: Record<string, number>
}
export interface LogFacets {
  sampled: boolean // the window held more events than the server reads; counts cover the newest ones
  levels: { value: string; count: number }[]
  signatures: { value: string; template: string | null; count: number }[]
}
export type Interval = '1m' | '5m' | '1h' | '1d'

/** The filters shared by the list, the histogram and the facets. `from`/`to` are ISO times; `to` is exclusive. */
export interface LogFilters {
  from?: string
  to?: string
  level?: string
  q?: string
  signature?: string
}

const query = (params: Record<string, string | number | undefined>) => {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== '') qs.set(k, String(v))
  return qs.size ? `?${qs}` : ''
}
const logsPath = (id: string) => `${projectPath(id)}/logs`

export const listLogs = (id: string, f: LogFilters, opts: { cursor?: string | null; sort: 'asc' | 'desc'; limit?: number }) =>
  request<LogPage>('GET', `${logsPath(id)}${query({ ...f, cursor: opts.cursor ?? undefined, sort: opts.sort, limit: opts.limit ?? 100 })}`)

export const getLog = (id: string, eventId: string) => request<LogDetail>('GET', `${logsPath(id)}/${eventId}`)

export const logStats = (id: string, f: LogFilters & { from: string; to: string }, interval: Interval) =>
  request<{ interval: Interval; sampled: boolean; buckets: StatsBucket[] }>('GET', `${logsPath(id)}/stats${query({ ...f, interval })}`)

export const logFacets = (id: string, f: LogFilters) => request<LogFacets>('GET', `${logsPath(id)}/facets${query({ ...f })}`)
