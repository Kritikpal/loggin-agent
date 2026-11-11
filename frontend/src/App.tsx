import { useEffect, useRef, useState, type ReactNode } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Badge, Box, Button, Callout, Card, Code, DataList, Flex, Heading, SegmentedControl, Select, Skeleton, Text, TextArea } from '@radix-ui/themes'
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
import {
  ApiError,
  listProjects,
  resumeWorkflow,
  sendChat,
  settleConflict,
  type Campaign,
  type ChatResponse,
  type ConflictChoice,
  type MemoryConflict,
  type ProjectSummary,
  type ProposedAction,
  type ToolCall,
  type WorkflowStatus,
} from './api'
import { Logs } from './Logs'
import { ManageProjects } from './Projects'

const MAX_LEN = 2000 // ChatRequest.message limit
const EXAMPLE_GROUPS: { title: string; questions: string[] }[] = [
  {
    title: 'Find problems',
    questions: ['What errors or exceptions happened in the last 24 hours?', 'Which error occurs most often, and when did it first appear?'],
  },
  {
    title: 'Trace activity',
    questions: ['Who were the most active users or initiators today?', 'What happened in the 10 minutes before the latest error?'],
  },
  {
    title: 'Understand the data',
    questions: ['How many log lines are indexed, and what time range do they cover?', 'Summarise activity per hour for the last day.'],
  },
]
const STATUS: Record<Exclude<WorkflowStatus, 'COMPLETED'>, { color: 'amber' | 'jade' | 'gray' | 'red'; label: string }> = {
  WAITING_APPROVAL: { color: 'amber', label: 'Awaiting your approval' },
  EXECUTED: { color: 'jade', label: 'Action executed' },
  REJECTED: { color: 'gray', label: 'Rejected' },
  INVALID_ACTION: { color: 'red', label: 'Action rejected as invalid' },
  BLOCKED: { color: 'red', label: 'Blocked by policy' },
}
const ERROR_TITLE: Record<string, string> = {
  network: 'Connection lost.',
  unreachable: 'Log Agent API unreachable.',
  unauthorized: 'Access denied.',
  ai_unavailable: 'Analysis engine unavailable.',
  validation_error: 'Question not accepted.',
  not_found: 'Session not found.',
  conflict: 'Already resolved.',
  internal_error: 'Internal error.',
}

type Decision = 'APPROVED' | 'REJECTED'
type DecisionState = { pending: Decision } | { reply: ChatResponse } | { error: ApiError }
// SETTLED: already settled elsewhere, e.g. the manager restated the correction in chat (the API answers 404 or 409)
type ConflictState = { pending: ConflictChoice } | { done: ConflictChoice | 'SETTLED' } | { error: ApiError }
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
  const [conflictStates, setConflictStates] = useState<Record<string, ConflictState>>({})
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null)
  const [projectsError, setProjectsError] = useState<ApiError | null>(null)
  const [projectId, setProjectId] = useState<string | null>(null)
  const [view, setView] = useState<'chat' | 'logs'>('chat')
  const [logsSeen, setLogsSeen] = useState(false) // the Logs tab stays mounted once opened, so its filters and place survive a tab switch
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [turns, decisions, conflictStates])

  // Projects live in the database and change through the Projects dialog, which asks for a reload.
  // `removedId` is a project that was just deleted: an open chat about it ends.
  function loadProjects(removedId?: string) {
    return listProjects()
      .then((list) => {
        setProjects(list)
        setProjectsError(null)
        const usable = list.filter((p) => p.status === 'ok')
        if (removedId && removedId === projectId) {
          newChat()
          setProjectId(usable.length === 1 ? usable[0].id : null)
        } else if (projectId === null && usable.length === 1) setProjectId(usable[0].id)
      })
      .catch((e) => setProjectsError(toApiError(e)))
  }

  useEffect(() => {
    loadProjects()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- first load only; later loads are asked for by the dialog
  }, [])

  const patchTurn = (id: number, patch: Partial<Turn>) => setTurns((ts) => ts.map((t) => (t.id === id ? { ...t, ...patch } : t)))

  async function send(raw: string, retryId?: number) {
    const message = raw.trim()
    if (!message || message.length > MAX_LEN || busy || !projectId) return
    const id = retryId ?? Date.now()
    setBusy(true)
    if (retryId) patchTurn(id, { error: undefined })
    else {
      setTurns((ts) => [...ts, { id, message }])
      setDraft('')
    }
    try {
      const reply = await sendChat(projectId, message, conversationId)
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

  async function settle(memoryId: string, choice: ConflictChoice) {
    setConflictStates((s) => ({ ...s, [memoryId]: { pending: choice } }))
    try {
      await settleConflict(memoryId, choice)
      setConflictStates((s) => ({ ...s, [memoryId]: { done: choice } }))
    } catch (e) {
      const err = toApiError(e)
      const gone = err.status === 404 || err.status === 409
      setConflictStates((s) => ({ ...s, [memoryId]: gone ? { done: 'SETTLED' } : { error: err } }))
    }
  }

  // Every reply carries the open conflicts; each is shown once per chat, under the first reply that had it.
  const conflictTurn = new Map<string, number>()
  for (const t of turns) for (const c of t.reply?.memory_conflicts ?? []) if (!conflictTurn.has(c.id)) conflictTurn.set(c.id, t.id)

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
    setConflictStates({})
    setDraft('')
  }

  // A chat belongs to one project: switching starts a new conversation.
  function chooseProject(id: string) {
    if (id === projectId) return
    newChat()
    setProjectId(id)
  }

  const tooLong = draft.length > MAX_LEN
  const canSend = !busy && !tooLong && draft.trim().length > 0 && projectId !== null

  return (
    <Flex direction="column" height="100%">
      <Flex px="4" py="3" gap="2" align="center" justify="between" wrap="wrap" className="topbar">
        <Flex align="center" gap="3" wrap="wrap">
          <Flex align="center" gap="2">
            <span className={`pulse ${projectsError ? 'down' : ''}`} aria-hidden />
            <Heading size="3" className="mono">
              log-agent
            </Heading>
          </Flex>
          <Text size="1" color="gray">
            Source
          </Text>
          <ProjectPicker projects={projects} error={projectsError} value={projectId} disabled={busy} onChange={chooseProject} />
          {conversationId && (
            <Code size="1" variant="ghost" color="gray" title={`Session ${conversationId}`}>
              session {conversationId.slice(0, 8)}
            </Code>
          )}
        </Flex>
        <SegmentedControl.Root size="1" value={view} onValueChange={(v) => {
            setView(v as 'chat' | 'logs')
            if (v === 'logs') setLogsSeen(true)
          }}>
          <SegmentedControl.Item value="chat">Investigate</SegmentedControl.Item>
          <SegmentedControl.Item value="logs">Logs</SegmentedControl.Item>
        </SegmentedControl.Root>
        <Flex gap="2">
          <ManageProjects projects={projects} onChanged={loadProjects} />
          <Button variant="soft" color="gray" onClick={newChat} disabled={busy || turns.length === 0}>
            <PlusIcon /> New investigation
          </Button>
        </Flex>
      </Flex>

      <Box flexGrow="1" style={{ overflowY: 'auto' }}>
        <Box mx="auto" px="4" py="5" style={{ maxWidth: view === 'logs' ? 1200 : 760 }}>
          {logsSeen && (
            <div hidden={view !== 'logs'}>
              <Logs project={projects?.find((p) => p.id === projectId) ?? null} />
            </div>
          )}
          {view === 'chat' && turns.length === 0 && (
            <EmptyState onPick={send} disabled={busy || !projectId} project={projects?.find((p) => p.id === projectId) ?? null} />
          )}
          {view === 'chat' && turns.map((turn, i) => (
            <Flex key={turn.id} direction="column" gap="3" mb="6">
              <Flex gap="2" className="query">
                <span className="prompt mono" aria-hidden>
                  &gt;
                </span>
                <Box flexGrow="1">
                  <Text as="p" size="2" className="mono" style={preWrap}>
                    {turn.message}
                  </Text>
                  <Text size="1" color="gray">
                    {new Date(turn.id).toLocaleTimeString()}
                  </Text>
                </Box>
              </Flex>
              {turn.reply ? (
                <Reply reply={turn.reply} decisions={decisions} outcomeFor={outcomeFor} onDecide={decide}>
                  {turn.reply.memory_conflicts
                    .filter((c) => conflictTurn.get(c.id) === turn.id)
                    .map((c) => (
                      <ConflictCard key={c.id} conflict={c} state={conflictStates[c.id]} onSettle={(choice) => settle(c.id, choice)} />
                    ))}
                </Reply>
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
                      Start new investigation
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

      {view === 'chat' && <Box px="4" py="3" className="composer">
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
                aria-label="Question" className="mono"
                placeholder={projectId ? 'Ask about your logs, e.g. "errors from the last hour"' : 'Select a log source to begin'}
                disabled={!projectId}
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
              <PaperPlaneIcon /> Run query
            </Button>
          </Flex>
          <Flex justify="between" mt="1">
            <Text size="1" color="gray">
              Enter to run · Shift+Enter for a new line · answers come from the indexed logs
            </Text>
            <Text size="1" color={tooLong ? 'red' : 'gray'}>
              {draft.length}/{MAX_LEN}
            </Text>
          </Flex>
        </form>
      </Box>}
    </Flex>
  )
}

type ProjectPickerProps = {
  projects: ProjectSummary[] | null
  error: ApiError | null
  value: string | null
  disabled: boolean
  onChange: (id: string) => void
}

/** Choose the project a chat is about. An invalid project is listed but cannot be chosen; hovering shows why. */
function ProjectPicker({ projects, error, value, disabled, onChange }: ProjectPickerProps) {
  if (error) {
    return (
      <Text size="1" color="red" title={error.message}>
        Log sources unavailable
      </Text>
    )
  }
  if (projects === null) return <Skeleton width="160px" height="24px" />
  if (projects.length === 0) {
    return (
      <Text size="1" color="gray">
        No log sources yet. Add one under Projects
      </Text>
    )
  }
  return (
    <Select.Root value={value ?? ''} onValueChange={onChange} disabled={disabled} size="1">
      <Select.Trigger placeholder="Select a log source" aria-label="Log source" />
      <Select.Content>
        {projects.map((p) => (
          <Select.Item key={p.id} value={p.id} disabled={p.status !== 'ok'} title={p.error ?? undefined}>
            {p.title}
            {p.status !== 'ok' ? ' (invalid)' : p.title.toLowerCase() === p.type ? '' : ` · ${p.type}`}
          </Select.Item>
        ))}
      </Select.Content>
    </Select.Root>
  )
}

function EmptyState({ onPick, disabled, project }: { onPick: (m: string) => void; disabled: boolean; project: ProjectSummary | null }) {
  return (
    <Flex direction="column" gap="4" pt="7">
      <Flex direction="column" gap="2">
        <Text size="1" color="jade" className="mono">
          {project ? `${project.title} · ${project.type} · ${project.file_count} files indexed` : 'no source selected'}
        </Text>
        <Heading size="7">What do you want to know about your logs?</Heading>
        <Text size="2" color="gray" style={{ maxWidth: '62ch' }}>
          Ask in plain English. The agent searches the indexed log lines, shows the queries it ran, and cites what it found. Anything that would
          change something comes back as a proposal you approve first.
        </Text>
      </Flex>
      <Flex direction="column" gap="4" mt="2">
        {EXAMPLE_GROUPS.map((g) => (
          <Flex key={g.title} direction="column" gap="2" align="start">
            <Text size="1" weight="medium" color="gray" className="mono" style={{ textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              {g.title}
            </Text>
            {g.questions.map((q) => (
              <button key={q} type="button" className="suggest" disabled={disabled} onClick={() => onPick(q)}>
                <span className="mono" aria-hidden>
                  &gt;
                </span>{' '}
                {q}
              </button>
            ))}
          </Flex>
        ))}
      </Flex>
    </Flex>
  )
}

function Thinking() {
  return (
    <Flex direction="column" gap="2" width="85%" aria-live="polite" aria-busy>
      <Text size="1" color="gray">
        Searching logs and reasoning over the matches...
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
  children?: ReactNode
}

function Reply({ reply, decisions, outcomeFor, onDecide, children }: ReplyProps) {
  const status = reply.status === 'COMPLETED' ? null : STATUS[reply.status]
  const threadId = reply.thread_id
  return (
    <Flex direction="column" gap="3" className="answer">
      <Text size="1" color="gray" className="mono">
        AGENT
      </Text>
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
      {children}
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
          Proposed action: create campaign
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
            <Callout.Text>{approved ? 'Approved. The action was carried out.' : 'Rejected. Nothing was changed.'}</Callout.Text>
          </Callout.Root>
        ) : (
          <Flex gap="2">
            <Button loading={pending === 'APPROVED'} disabled={!!pending} onClick={() => onDecide('APPROVED')}>
              <CheckIcon /> Approve and run
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

const CONFLICT_OUTCOME: Record<ConflictChoice | 'SETTLED', string> = {
  USE_NEW: 'Updated. The agent will use your new note from now on.',
  KEEP_EARLIER: 'Kept the earlier note. The new one was discarded.',
  SETTLED: 'Already settled. Nothing more to do.',
}

interface ConflictProps {
  conflict: MemoryConflict
  state?: ConflictState
  onSettle: (choice: ConflictChoice) => void
}

function ConflictCard({ conflict: c, state, onSettle }: ConflictProps) {
  const pending = state && 'pending' in state ? state.pending : null
  const done = state && 'done' in state ? state.done : null
  const error = state && 'error' in state ? state.error : null
  return (
    <Card size="2">
      <Flex direction="column" gap="3">
        <Flex align="center" gap="2" wrap="wrap">
          <Badge color="amber">Memory conflict</Badge>
          <Text size="2" color="gray">
            Two notes disagree. Which should the agent trust?
          </Text>
        </Flex>
        <DataList.Root size="2">
          {c.earlier_content && <Field label="Earlier note">{c.earlier_content}</Field>}
          <Field label="Your correction">&ldquo;{c.evidence}&rdquo;</Field>
          <Field label="New note">{c.content}</Field>
        </DataList.Root>
        {done ? (
          <Callout.Root size="1" color={done === 'USE_NEW' ? 'jade' : 'gray'}>
            <Callout.Icon>{done === 'USE_NEW' ? <CheckCircledIcon /> : <InfoCircledIcon />}</Callout.Icon>
            <Callout.Text>{CONFLICT_OUTCOME[done]}</Callout.Text>
          </Callout.Root>
        ) : (
          <Flex gap="2" wrap="wrap">
            <Button loading={pending === 'USE_NEW'} disabled={!!pending} onClick={() => onSettle('USE_NEW')}>
              <CheckIcon /> Trust the new note
            </Button>
            <Button
              variant="soft"
              color="gray"
              loading={pending === 'KEEP_EARLIER'}
              disabled={!!pending}
              onClick={() => onSettle('KEEP_EARLIER')}
            >
              Keep the earlier note
            </Button>
          </Flex>
        )}
        {error && <ErrorCallout error={error} />}
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
          Evidence: {calls.length} {calls.length === 1 ? 'query' : 'queries'} run against the logs
        </Text>
      </summary>
      <Flex direction="column" gap="2" mt="2">
        {calls.map((c) => (
          <Box key={c.order} p="3" style={{ background: 'var(--gray-a2)', borderRadius: 'var(--radius-3)' }}>
            <Flex justify="between" align="center" mb="2">
              <Code size="2">{c.name}</Code>
              <Badge color={c.status === 'success' ? 'jade' : 'red'}>{c.status}</Badge>
            </Flex>
            <Text size="1" color="gray">Query parameters</Text>
            <pre>
              <Code size="1" variant="ghost">{JSON.stringify(c.args, null, 2)}</Code>
            </pre>
            {c.result && (
              <>
                <Text size="1" color="gray">Matching log output</Text>
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
          <Text weight="bold">{ERROR_TITLE[error.code] ?? 'Query failed.'}</Text> {error.message}
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
