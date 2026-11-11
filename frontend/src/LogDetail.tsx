import { useEffect, useState } from 'react'
import { Badge, Button, Callout, DataList, Dialog, Flex, Skeleton, Text } from '@radix-ui/themes'
import { CopyIcon, ExclamationTriangleIcon } from '@radix-ui/react-icons'
import { getLog, type LogDetail as Detail } from './api'
import { levelColor } from './levelColor'

const AUDIT_LABELS: Record<string, string> = {
  audit_action: 'Action',
  audit_result: 'Result',
  audit_actor: 'Actor',
  audit_target: 'Target',
  audit_user: 'User',
  audit_application: 'Application',
}

interface Props {
  projectId: string
  eventId: string | null
  onClose: () => void
}

const show = (v: unknown) => (typeof v === 'string' ? v : JSON.stringify(v))

/** Where the event came from, in words: "wso2carbon.log, line 5940" rather than the raw locator JSON. */
function origin(e: Detail) {
  const file = typeof e.locator.file === 'string' ? e.locator.file : null
  const line = e.locator.line
  if (file) return line ? `${file}, line ${String(line)}` : file
  return e.locator.kind === 'push' ? 'Pushed by an application' : e.locator.kind === 'upload' ? 'Uploaded log file' : e.source
}

/** One whole event: its text with line breaks kept, the audit facts first, then the other parsed fields. */
export function LogDetail({ projectId, eventId, onClose }: Props) {
  const [loaded, setLoaded] = useState<{ id: string; event?: Detail; error?: string } | null>(null)
  const [copied, setCopied] = useState<'ok' | 'failed' | null>(null)

  useEffect(() => {
    if (!eventId) return
    let current = true
    getLog(projectId, eventId)
      .then((event) => current && setLoaded({ id: eventId, event }))
      .catch((e: unknown) => current && setLoaded({ id: eventId, error: e instanceof Error ? e.message : String(e) }))
    return () => {
      current = false
    }
  }, [projectId, eventId])

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied('ok')
    } catch {
      setCopied('failed')
    }
    setTimeout(() => setCopied(null), 2000)
  }

  const state = loaded && loaded.id === eventId ? loaded : null
  const event = state?.event
  const entries = event ? Object.entries(event.fields) : []
  const audit = entries.filter(([k]) => k in AUDIT_LABELS)
  const other = entries.filter(([k]) => !(k in AUDIT_LABELS))
  return (
    <Dialog.Root open={eventId !== null} onOpenChange={(open) => !open && onClose()}>
      <Dialog.Content maxWidth="860px">
        <Dialog.Title>
          {event ? (
            <Flex gap="2" align="center" wrap="wrap">
              {event.level && (
                <Badge variant="solid" style={{ background: levelColor(event.level.toUpperCase()) }}>
                  {event.level}
                </Badge>
              )}
              <span>{event.occurred_at ? new Date(event.occurred_at).toLocaleString() : 'Log event'}</span>
            </Flex>
          ) : (
            'Log event'
          )}
        </Dialog.Title>
        <Dialog.Description size="1" color="gray" mb="3">
          {event ? origin(event) : 'Loading from the index'}
        </Dialog.Description>
        {state?.error && (
          <Callout.Root color="red" size="1">
            <Callout.Icon>
              <ExclamationTriangleIcon />
            </Callout.Icon>
            <Callout.Text>{state.error}</Callout.Text>
          </Callout.Root>
        )}
        {!state && <Skeleton height="120px" />}
        {event && (
          <Flex direction="column" gap="3">
            {audit.length > 0 && (
              <DataList.Root size="2">
                {audit.map(([k, v]) => (
                  <DataList.Item key={k}>
                    <DataList.Label minWidth="96px">{AUDIT_LABELS[k]}</DataList.Label>
                    <DataList.Value className="mono">{show(v)}</DataList.Value>
                  </DataList.Item>
                ))}
              </DataList.Root>
            )}
            <pre className="log-text">{event.raw_text}</pre>
            {event.signature_template && (
              <Text size="1" color="gray" className="mono">
                Event kind: {event.signature_template}
              </Text>
            )}
            {other.length > 0 && (
              <details>
                <summary>
                  <Text size="1" color="gray">
                    Other fields ({other.length})
                  </Text>
                </summary>
                <DataList.Root size="2" mt="2">
                  {other.map(([k, v]) => (
                    <DataList.Item key={k}>
                      <DataList.Label minWidth="96px">{k}</DataList.Label>
                      <DataList.Value className="mono" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                        {show(v)}
                      </DataList.Value>
                    </DataList.Item>
                  ))}
                </DataList.Root>
              </details>
            )}
          </Flex>
        )}
        <Flex gap="2" justify="end" align="center" mt="4">
          {copied && (
            <Text size="1" color={copied === 'ok' ? 'jade' : 'red'} role="status">
              {copied === 'ok' ? 'Copied' : 'Could not copy'}
            </Text>
          )}
          {event && (
            <Button variant="soft" color="gray" onClick={() => copy(event.raw_text)}>
              <CopyIcon /> Copy text
            </Button>
          )}
          <Dialog.Close>
            <Button variant="soft">Close</Button>
          </Dialog.Close>
        </Flex>
      </Dialog.Content>
    </Dialog.Root>
  )
}
