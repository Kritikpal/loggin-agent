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

async function request<T>(method: 'POST' | 'DELETE', path: string, body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(`/api/v1${path}`, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
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

export const sendChat = (message: string, conversationId: string | null) =>
  request<ChatResponse>('POST', '/ai/chat', { message, conversation_id: conversationId })

export const resumeWorkflow = (threadId: string, decision: 'APPROVED' | 'REJECTED') =>
  request<ChatResponse>('POST', `/ai/workflows/${threadId}/resume`, { decision })

export type ConflictChoice = 'USE_NEW' | 'KEEP_EARLIER'

/** Settle a flagged memory: use the correction, or delete it and keep the earlier note. */
export const settleConflict = (memoryId: string, choice: ConflictChoice) =>
  choice === 'USE_NEW'
    ? request<unknown>('POST', `/ai/memories/${memoryId}/accept`)
    : request<unknown>('DELETE', `/ai/memories/${memoryId}`)
