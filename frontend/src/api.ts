// Types mirror app/schemas/ai.py and app/schemas/campaign.py. Decimals arrive as strings.

export type WorkflowStatus = 'COMPLETED' | 'WAITING_APPROVAL' | 'EXECUTED' | 'REJECTED' | 'INVALID_ACTION'

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

export interface ChatResponse {
  response: string
  tool_calls: ToolCall[]
  status: WorkflowStatus
  thread_id: string | null
  proposed_action: ProposedAction | null
  campaign: Campaign | null
  conversation_id: string | null
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

async function post<T>(path: string, body: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(`/api/v1${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, 'network', 'Could not reach the server. Check your connection and try again.')
  }
  const data = await res.json().catch(() => null)
  if (res.ok && data) return data as T
  const err = data?.error // backend envelope: {"error": {code, message, details}}
  if (err) throw new ApiError(res.status, err.code, err.message, err.details)
  throw new ApiError(res.status, 'unreachable', `The API is not responding (HTTP ${res.status}). Is the backend running on port 8000?`)
}

export const sendChat = (message: string, conversationId: string | null) =>
  post<ChatResponse>('/ai/chat', { message, conversation_id: conversationId })

export const resumeWorkflow = (threadId: string, decision: 'APPROVED' | 'REJECTED') =>
  post<ChatResponse>(`/ai/workflows/${threadId}/resume`, { decision })
