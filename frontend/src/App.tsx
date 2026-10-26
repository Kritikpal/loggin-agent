import { useEffect, useRef, useState, type ReactNode } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Badge, Box, Button, Callout, Card, Code, DataList, Flex, Heading, Skeleton, Text, TextArea } from '@radix-ui/themes'
import {
  CheckCircledIcon,
  CheckIcon,
  Cross2Icon,
  ExclamationTriangleIcon,
  InfoCircledIcon,
  PaperPlaneIcon,
  PlusIcon,
  ReloadIcon,
} from '@radix-ui/react-icons'
import { ApiError, resumeWorkflow, sendChat, type Campaign, type ChatResponse, type ProposedAction, type ToolCall, type WorkflowStatus } from './api'

const MAX_LEN = 2000 // ChatRequest.message limit
const EXAMPLES = [
  'Which products are running low on stock?',
  'How did revenue change over the last two weeks?',
  'Create a 15% discount campaign for Shirts',
]
const STATUS: Record<Exclude<WorkflowStatus, 'COMPLETED'>, { color: 'amber' | 'jade' | 'gray' | 'red'; label: string }> = {
  WAITING_APPROVAL: { color: 'amber', label: 'Needs approval' },
  EXECUTED: { color: 'jade', label: 'Campaign created' },
  REJECTED: { color: 'gray', label: 'Rejected' },
  INVALID_ACTION: { color: 'red', label: 'Action not valid' },
}
const ERROR_TITLE: Record<string, string> = {
  network: 'Offline.',
  unreachable: 'Server unreachable.',
  ai_unavailable: 'AI unavailable.',
  validation_error: 'Invalid message.',
  not_found: 'Not found.',
  conflict: 'Already decided.',
  internal_error: 'Server error.',
}

type Decision = 'APPROVED' | 'REJECTED'
type DecisionState = { pending: Decision } | { reply: ChatResponse } | { error: ApiError }
interface Turn {
  id: number
  message: string
  reply?: ChatResponse
  error?: ApiError
}

const toApiError = (e: unknown) => (e instanceof ApiError ? e : new ApiError(0, 'unknown', String(e)))
const preWrap = { whiteSpace: 'pre-wrap' } as const

export default function App() {
  const [turns, setTurns] = useState<Turn[]>([])
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [decisions, setDecisions] = useState<Record<string, DecisionState>>({})
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [turns, decisions])

  const patchTurn = (id: number, patch: Partial<Turn>) => setTurns((ts) => ts.map((t) => (t.id === id ? { ...t, ...patch } : t)))

  async function send(raw: string, retryId?: number) {
    const message = raw.trim()
    if (!message || message.length > MAX_LEN || busy) return
    const id = retryId ?? Date.now()
    setBusy(true)
    if (retryId) patchTurn(id, { error: undefined })
    else {
      setTurns((ts) => [...ts, { id, message }])
      setDraft('')
    }
    try {
      const reply = await sendChat(message, conversationId)
      setConversationId(reply.conversation_id)
      patchTurn(id, { reply })
    } catch (e) {
      patchTurn(id, { error: toApiError(e) })
    } finally {
      setBusy(false)
    }
  }

  async function decide(threadId: string, decision: Decision) {
    setDecisions((d) => ({ ...d, [threadId]: { pending: decision } }))
    try {
      const reply = await resumeWorkflow(threadId, decision)
      setDecisions((d) => ({ ...d, [threadId]: { reply } }))
    } catch (e) {
      setDecisions((d) => ({ ...d, [threadId]: { error: toApiError(e) } }))
    }
  }

  // A proposal is settled by its button, or by a later "yes"/"no" chat turn that resumed the same thread.
  function outcomeFor(threadId: string) {
    const d = decisions[threadId]
    if (d && 'reply' in d) return d.reply
    return turns.find((t) => t.reply?.thread_id === threadId && (t.reply.status === 'EXECUTED' || t.reply.status === 'REJECTED'))?.reply
  }

  function editTurn(turn: Turn) {
    setTurns((ts) => ts.filter((t) => t.id !== turn.id))
    setDraft(turn.message)
  }

  function newChat() {
    setTurns([])
    setConversationId(null)
    setDecisions({})
    setDraft('')
  }

  const tooLong = draft.length > MAX_LEN
  const canSend = !busy && !tooLong && draft.trim().length > 0

  return (
    <Flex direction="column" height="100%">
      <Flex px="4" py="3" align="center" justify="between" style={{ borderBottom: '1px solid var(--gray-a5)' }}>
        <Flex align="center" gap="3">
          <Heading size="3">Sales assistant</Heading>
          {conversationId && (
            <Code size="1" variant="ghost" color="gray" title={conversationId}>
              {conversationId.slice(0, 8)}
            </Code>
          )}
        </Flex>
        <Button variant="soft" color="gray" onClick={newChat} disabled={busy || turns.length === 0}>
          <PlusIcon /> New chat
        </Button>
      </Flex>

      <Box flexGrow="1" style={{ overflowY: 'auto' }}>
        <Box mx="auto" px="4" py="5" style={{ maxWidth: 760 }}>
          {turns.length === 0 && <EmptyState onPick={send} disabled={busy} />}
          {turns.map((turn, i) => (
            <Flex key={turn.id} direction="column" gap="3" mb="6">
              <Flex justify="end">
                <Box px="3" py="2" maxWidth="85%" style={{ background: 'var(--accent-a3)', borderRadius: 'var(--radius-4)' }}>
                  <Text as="p" size="2" style={preWrap}>
                    {turn.message}
                  </Text>
                </Box>
              </Flex>
              {turn.reply ? (
                <Reply reply={turn.reply} decisions={decisions} outcomeFor={outcomeFor} onDecide={decide} />
              ) : turn.error ? (
                <ErrorCallout error={turn.error}>
                  {i === turns.length - 1 && turn.error.code !== 'validation_error' && (
                    <Button size="1" variant="soft" color="red" disabled={busy} onClick={() => send(turn.message, turn.id)}>
                      <ReloadIcon /> Retry
                    </Button>
                  )}
                  <Button size="1" variant="ghost" color="gray" onClick={() => editTurn(turn)}>
                    Edit message
                  </Button>
                  {turn.error.code === 'not_found' && conversationId && (
                    <Button size="1" variant="ghost" color="gray" onClick={newChat}>
                      Start new chat
                    </Button>
                  )}
                </ErrorCallout>
              ) : (
                <Thinking />
              )}
            </Flex>
          ))}
          <div ref={endRef} />
        </Box>
      </Box>

      <Box px="4" py="3" style={{ borderTop: '1px solid var(--gray-a5)' }}>
        <form
          style={{ maxWidth: 760, margin: '0 auto' }}
          onSubmit={(e) => {
            e.preventDefault()
            send(draft)
          }}
        >
          <Flex gap="2" align="end">
            <Box flexGrow="1">
              <TextArea
                aria-label="Message"
                placeholder="Ask a question or request a campaign"
                rows={2}
                value={draft}
                color={tooLong ? 'red' : undefined}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault()
                    send(draft)
                  }
                }}
              />
            </Box>
            <Button type="submit" size="3" loading={busy} disabled={!canSend}>
              <PaperPlaneIcon /> Send
            </Button>
          </Flex>
          <Flex justify="between" mt="1">
            <Text size="1" color="gray">
              Enter to send, Shift+Enter for a new line
            </Text>
            <Text size="1" color={tooLong ? 'red' : 'gray'}>
              {draft.length}/{MAX_LEN}
            </Text>
          </Flex>
        </form>
      </Box>
    </Flex>
  )
}

function EmptyState({ onPick, disabled }: { onPick: (m: string) => void; disabled: boolean }) {
  return (
    <Flex direction="column" gap="3" pt="8">
      <Heading size="6">Ask about sales, stock or campaigns.</Heading>
      <Text size="2" color="gray" style={{ maxWidth: '60ch' }}>
        Questions are answered right away. Requests to change something come back as a proposal for you to approve or reject.
      </Text>
      <Flex direction="column" gap="2" align="start" mt="2">
        {EXAMPLES.map((e) => (
          <Button key={e} variant="surface" color="gray" disabled={disabled} onClick={() => onPick(e)}>
            {e}
          </Button>
        ))}
      </Flex>
    </Flex>
  )
}

function Thinking() {
  return (
    <Flex direction="column" gap="2" width="85%" aria-live="polite" aria-busy>
      <Text size="1" color="gray">
        Thinking...
      </Text>
      <Skeleton height="14px" width="90%" />
      <Skeleton height="14px" width="72%" />
      <Skeleton height="14px" width="40%" />
    </Flex>
  )
}

interface ReplyProps {
  reply: ChatResponse
  decisions: Record<string, DecisionState>
  outcomeFor: (threadId: string) => ChatResponse | undefined
  onDecide: (threadId: string, d: Decision) => void
}

function Reply({ reply, decisions, outcomeFor, onDecide }: ReplyProps) {
  const status = reply.status === 'COMPLETED' ? null : STATUS[reply.status]
  const threadId = reply.thread_id
  return (
    <Flex direction="column" gap="3" maxWidth="85%">
      {status && (
        <Badge color={status.color} style={{ alignSelf: 'flex-start' }}>
          {status.label}
        </Badge>
      )}
      <Text as="div" size="2" className="md">
        <Markdown remarkPlugins={[remarkGfm]}>{reply.response}</Markdown>
      </Text>
      {reply.status === 'WAITING_APPROVAL' && reply.proposed_action && threadId && (
        <ProposalCard
          action={reply.proposed_action}
          state={decisions[threadId]}
          outcome={outcomeFor(threadId)}
          onDecide={(d) => onDecide(threadId, d)}
        />
      )}
      {reply.campaign && <CampaignCard campaign={reply.campaign} />}
      {reply.tool_calls.length > 0 && <ToolCalls calls={reply.tool_calls} />}
    </Flex>
  )
}

interface ProposalProps {
  action: ProposedAction
  state?: DecisionState
  outcome?: ChatResponse
  onDecide: (d: Decision) => void
}

function ProposalCard({ action, state, outcome, onDecide }: ProposalProps) {
  const pending = state && 'pending' in state ? state.pending : null
  const error = state && 'error' in state ? state.error : null
  const approved = outcome?.status === 'EXECUTED'
  return (
    <Card size="2">
      <Flex direction="column" gap="3">
        <Text size="2" weight="medium">
          Proposed campaign
        </Text>
        <DataList.Root size="2">
          <Field label="Name">{action.name}</Field>
          <Field label="Category">{action.category}</Field>
          <Field label="Discount">{Number(action.discount_percent)}%</Field>
          <Field label="Message">{action.message}</Field>
        </DataList.Root>
        {outcome ? (
          <Callout.Root size="1" color={approved ? 'jade' : 'gray'}>
            <Callout.Icon>{approved ? <CheckCircledIcon /> : <InfoCircledIcon />}</Callout.Icon>
            <Callout.Text>{approved ? 'Approved. The campaign was created.' : 'Rejected. Nothing was changed.'}</Callout.Text>
          </Callout.Root>
        ) : (
          <Flex gap="2">
            <Button loading={pending === 'APPROVED'} disabled={!!pending} onClick={() => onDecide('APPROVED')}>
              <CheckIcon /> Approve
            </Button>
            <Button variant="soft" color="red" loading={pending === 'REJECTED'} disabled={!!pending} onClick={() => onDecide('REJECTED')}>
              <Cross2Icon /> Reject
            </Button>
          </Flex>
        )}
        {error && <ErrorCallout error={error} />}
        {/* a chat "yes" shows its campaign in its own turn; only the button result is shown here */}
        {state && 'reply' in state && state.reply.campaign && <CampaignCard campaign={state.reply.campaign} />}
      </Flex>
    </Card>
  )
}

function CampaignCard({ campaign: c }: { campaign: Campaign }) {
  return (
    <Card size="2">
      <Flex direction="column" gap="3">
        <Flex justify="between" align="center" gap="3">
          <Text size="2" weight="medium">
            {c.name}
          </Text>
          <Badge color="jade">{c.status}</Badge>
        </Flex>
        <DataList.Root size="2">
          <Field label="Category">{c.category ?? 'Any'}</Field>
          <Field label="Discount">{Number(c.discount_percent)}%</Field>
          <Field label="Message">{c.message}</Field>
          <Field label="ID">
            <Code size="1">{c.id}</Code>
          </Field>
          <Field label="Created">{new Date(c.created_at).toLocaleString()}</Field>
        </DataList.Root>
      </Flex>
    </Card>
  )
}

function ToolCalls({ calls }: { calls: ToolCall[] }) {
  return (
    <details>
      <summary style={{ cursor: 'pointer' }}>
        <Text size="1" color="gray">
          {calls.length} tool {calls.length === 1 ? 'call' : 'calls'}
        </Text>
      </summary>
      <Flex direction="column" gap="2" mt="2">
        {calls.map((c) => (
          <Box key={c.order} p="3" style={{ background: 'var(--gray-a2)', borderRadius: 'var(--radius-3)' }}>
            <Flex justify="between" align="center" mb="2">
              <Code size="2">{c.name}</Code>
              <Badge color={c.status === 'success' ? 'jade' : 'red'}>{c.status}</Badge>
            </Flex>
            <Text size="1" color="gray">Arguments</Text>
            <pre>
              <Code size="1" variant="ghost">{JSON.stringify(c.args, null, 2)}</Code>
            </pre>
            {c.result && (
              <>
                <Text size="1" color="gray">Result</Text>
                <Box style={{ maxHeight: 220, overflowY: 'auto' }}>
                  <pre>
                    <Code size="1" variant="ghost">{c.result}</Code>
                  </pre>
                </Box>
              </>
            )}
          </Box>
        ))}
      </Flex>
    </details>
  )
}

function ErrorCallout({ error, children }: { error: ApiError; children?: ReactNode }) {
  const fields = Array.isArray(error.details) ? (error.details as { field: string; message: string }[]) : []
  return (
    <Callout.Root color="red" size="1" role="alert">
      <Callout.Icon>
        <ExclamationTriangleIcon />
      </Callout.Icon>
      <Flex direction="column" gap="2">
        <Callout.Text>
          <Text weight="bold">{ERROR_TITLE[error.code] ?? 'Request failed.'}</Text> {error.message}
        </Callout.Text>
        {fields.map((f) => (
          <Text key={f.field} size="1">
            {f.field}: {f.message}
          </Text>
        ))}
        {children && (
          <Flex gap="3" align="center">
            {children}
          </Flex>
        )}
      </Flex>
    </Callout.Root>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <DataList.Item>
      <DataList.Label minWidth="88px">{label}</DataList.Label>
      <DataList.Value style={preWrap}>{children}</DataList.Value>
    </DataList.Item>
  )
}
