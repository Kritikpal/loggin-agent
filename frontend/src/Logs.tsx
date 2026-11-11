import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Badge, Box, Button, Callout, Code, Flex, IconButton, SegmentedControl, Select, Table, Text, TextField } from '@radix-ui/themes'
import { Cross2Icon, ExclamationTriangleIcon, MagnifyingGlassIcon, ReloadIcon } from '@radix-ui/react-icons'
import {
  ApiError,
  listLogs,
  logFacets,
  logStats,
  type Interval,
  type LogFacets,
  type LogFilters,
  type LogItem,
  type ProjectSummary,
  type StatsBucket,
} from './api'
import { LogDetail } from './LogDetail'
import { LogHistogram } from './LogHistogram'
import { levelColor } from './levelColor'

// The presets count back from the newest indexed event (the data is historical), so they are named after the data.
const PRESETS = [
  { id: '1h', label: 'Final hour', ms: 3_600_000 },
  { id: '24h', label: 'Final 24h', ms: 86_400_000 },
  { id: '7d', label: 'Final 7 days', ms: 7 * 86_400_000 },
  { id: 'all', label: 'All data', ms: 0 },
] as const
type PresetId = (typeof PRESETS)[number]['id']
const LEVELS = ['ERROR', 'WARN', 'INFO', 'DEBUG']
const MAX_WINDOW_MS = 31 * 86_400_000 // the API refuses a wider range
const ANY = '__any__' // Radix Select cannot hold an empty value
const SPAN_FORMAT = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })

/** Pick a bucket size that keeps the histogram well under the API's 1000-bucket limit. */
function intervalFor(spanMs: number): Interval {
  if (spanMs <= 2 * 3_600_000) return '1m'
  if (spanMs <= 12 * 3_600_000) return '5m'
  if (spanMs <= 5 * 86_400_000) return '1h'
  return '1d'
}

const errorText = (e: unknown) => (e instanceof ApiError ? e.message : String(e))
const oneLine = (s: string) => s.split('\n', 1)[0]
const pad = (n: number) => String(n).padStart(2, '0')
/** An ISO time as the "YYYY-MM-DDTHH:mm" a datetime-local input wants, in the viewer's time zone. */
const toLocalInput = (iso: string) => {
  const d = new Date(iso)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

interface Props {
  project: ProjectSummary | null
}

/** Browse a project's indexed log lines: filter, see the shape over time, open one event whole. */
export function Logs({ project }: Props) {
  const [preset, setPreset] = useState<PresetId>('all')
  const [custom, setCustom] = useState<{ from: string; to: string } | null>(null) // typed dates or a clicked bar
  const [level, setLevel] = useState('')
  const [draftQ, setDraftQ] = useState('')
  const [q, setQ] = useState('')
  const [signature, setSignature] = useState<{ id: string; label: string } | null>(null)
  const [sort, setSort] = useState<'asc' | 'desc'>('desc')
  const [items, setItems] = useState<LogItem[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [buckets, setBuckets] = useState<StatsBucket[]>([])
  const [interval, setInterval_] = useState<Interval>('1h')
  const [facets, setFacets] = useState<LogFacets | null>(null)
  const [sampled, setSampled] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [reloads, setReloads] = useState(0)
  const seq = useRef(0) // a newer request makes older replies stale

  const projectId = project?.id
  const indexedTo = project?.indexed_to
  const indexedFrom = project?.indexed_from

  // `to` is exclusive, so it sits one second past the newest event.
  const range = useMemo(() => {
    if (!indexedTo || !indexedFrom) return null
    const to = new Date(new Date(indexedTo).getTime() + 1000).toISOString()
    if (custom) return custom
    const toMs = new Date(to).getTime()
    const ms = Math.min(PRESETS.find((p) => p.id === preset)!.ms || MAX_WINDOW_MS, MAX_WINDOW_MS)
    return { from: new Date(Math.max(toMs - ms, new Date(indexedFrom).getTime())).toISOString(), to }
  }, [indexedFrom, indexedTo, preset, custom])

  const filters: LogFilters | null = useMemo(
    () => (range ? { from: range.from, to: range.to, level, q, signature: signature?.id ?? '' } : null),
    [range, level, q, signature],
  )

  useEffect(() => {
    if (!projectId || !filters || !range) return
    const mine = ++seq.current
    const stale = () => mine !== seq.current
    const step = intervalFor(new Date(range.to).getTime() - new Date(range.from).getTime())
    setLoading(true)
    setError(null)
    Promise.all([
      listLogs(projectId, filters, { sort }),
      logStats(projectId, { ...filters, from: range.from, to: range.to }, step),
      logFacets(projectId, filters),
    ])
      .then(([page, stats, f]) => {
        if (stale()) return
        setItems(page.items)
        setCursor(page.next_cursor)
        setBuckets(stats.buckets)
        setInterval_(stats.interval)
        setFacets(f)
        setSampled(stats.sampled || f.sampled)
      })
      .catch((e: unknown) => !stale() && setError(errorText(e)))
      .finally(() => !stale() && setLoading(false))
  }, [projectId, filters, range, sort, reloads])

  const loadMore = useCallback(async () => {
    if (!projectId || !filters || !cursor) return
    const mine = seq.current
    setLoading(true)
    try {
      const page = await listLogs(projectId, filters, { sort, cursor })
      if (mine !== seq.current) return
      setItems((old) => [...old, ...page.items])
      setCursor(page.next_cursor)
    } catch (e) {
      setError(errorText(e))
    } finally {
      setLoading(false)
    }
  }, [projectId, filters, cursor, sort])

  if (!project) return <Notice>Select a log source to browse its logs.</Notice>
  if (!indexedFrom || !indexedTo || !range) return <Notice>Nothing is indexed for this source yet. Upload or push some logs under Projects.</Notice>

  /** A typed date moves that end; the other end follows when the range would be backwards or wider than the 31 days
   * the server accepts, keeping the width the viewer had (at most 31 days). */
  function setEdge(edge: 'from' | 'to', value: string) {
    const picked = new Date(value).getTime()
    if (!value || Number.isNaN(picked) || !range) return
    const width = Math.min(new Date(range.to).getTime() - new Date(range.from).getTime(), MAX_WINDOW_MS)
    let from = edge === 'from' ? picked : new Date(range.from).getTime()
    let to = edge === 'to' ? picked : new Date(range.to).getTime()
    if (to <= from || to - from > MAX_WINDOW_MS) {
      if (edge === 'from') to = from + width
      else from = to - width
    }
    setCustom({ from: new Date(from).toISOString(), to: new Date(to).toISOString() })
  }

  const total = buckets.reduce((n, b) => n + b.total, 0)
  const errors = buckets.reduce((n, b) => n + (b.by_level.ERROR ?? 0) + (b.by_level.FATAL ?? 0), 0)
  const spansMoreThanWindow = new Date(indexedTo).getTime() - new Date(indexedFrom).getTime() > MAX_WINDOW_MS
  const showSource = new Set(items.map((e) => e.source)).size > 1
  const showWho = items.some((e) => e.actor || e.action)
  const clearAll = () => {
    setLevel('')
    setQ('')
    setDraftQ('')
    setSignature(null)
  }

  return (
    <Flex direction="column" gap="3">
      <Flex gap="2" wrap="wrap" align="center">
        <SegmentedControl.Root
          size="1"
          style={{ maxWidth: '100%', overflowX: 'auto' }}
          value={custom ? 'custom' : preset}
          onValueChange={(v) => {
            setCustom(null)
            if (v !== 'custom') setPreset(v as PresetId)
          }}
        >
          {PRESETS.map((p) => (
            <SegmentedControl.Item key={p.id} value={p.id}>
              {p.id === 'all' && spansMoreThanWindow ? 'Latest 31 days' : p.label}
            </SegmentedControl.Item>
          ))}
          {custom && <SegmentedControl.Item value="custom">Custom</SegmentedControl.Item>}
        </SegmentedControl.Root>
        <Flex gap="1" align="center" wrap="wrap">
          <TextField.Root size="1" type="datetime-local" aria-label="From" value={toLocalInput(range.from)} min={toLocalInput(indexedFrom)} onChange={(e) => setEdge('from', e.target.value)} />
          <Text size="1" color="gray">
            to
          </Text>
          <TextField.Root size="1" type="datetime-local" aria-label="To" value={toLocalInput(range.to)} onChange={(e) => setEdge('to', e.target.value)} />
        </Flex>
        <Button size="1" variant="soft" color="gray" onClick={() => setReloads((n) => n + 1)} disabled={loading} aria-label="Refresh">
          <ReloadIcon />
        </Button>
      </Flex>

      <Flex gap="2" wrap="wrap" align="center">
        <form
          style={{ flexGrow: 1, minWidth: 220 }}
          onSubmit={(e) => {
            e.preventDefault()
            setQ(draftQ.trim())
          }}
        >
          <TextField.Root size="2" className="mono" placeholder="Find text in log lines, e.g. SocketTimeoutException" aria-label="Find text in log lines" value={draftQ} onChange={(e) => setDraftQ(e.target.value)}>
            <TextField.Slot>
              <MagnifyingGlassIcon />
            </TextField.Slot>
          </TextField.Root>
        </form>
        <Select.Root size="2" value={level || ANY} onValueChange={(v) => setLevel(v === ANY ? '' : v)}>
          <Select.Trigger aria-label="Level" />
          <Select.Content>
            <Select.Item value={ANY}>All levels</Select.Item>
            {LEVELS.map((l) => (
              <Select.Item key={l} value={l}>
                {l}
              </Select.Item>
            ))}
          </Select.Content>
        </Select.Root>
        <Select.Root size="2" value={sort} onValueChange={(v) => setSort(v as 'asc' | 'desc')}>
          <Select.Trigger aria-label="Order" />
          <Select.Content>
            <Select.Item value="desc">Newest first</Select.Item>
            <Select.Item value="asc">Oldest first</Select.Item>
          </Select.Content>
        </Select.Root>
      </Flex>

      <Flex gap="2" align="center" wrap="wrap" aria-live="polite">
        <Text size="2" weight="medium">
          {loading && items.length === 0 ? 'Loading…' : `${total.toLocaleString()}${sampled ? '+' : ''} events`}
        </Text>
        {errors > 0 && (
          <Text size="2" style={{ color: levelColor('ERROR') }}>
            {errors.toLocaleString()} errors
          </Text>
        )}
        <Text size="1" color="gray">
          {SPAN_FORMAT.format(new Date(range.from))} – {SPAN_FORMAT.format(new Date(range.to))}
        </Text>
        {level && <FilterBadge onClear={() => setLevel('')}>level {level}</FilterBadge>}
        {q && (
          <FilterBadge
            onClear={() => {
              setQ('')
              setDraftQ('')
            }}
          >
            text “{q}”
          </FilterBadge>
        )}
        {signature && <FilterBadge onClear={() => setSignature(null)}>kind: {signature.label.slice(0, 50)}</FilterBadge>}
        {(level || q || signature) && (
          <Button size="1" variant="ghost" color="gray" onClick={clearAll}>
            Clear all
          </Button>
        )}
      </Flex>

      {error && (
        <Callout.Root color="red" size="1" role="alert">
          <Callout.Icon>
            <ExclamationTriangleIcon />
          </Callout.Icon>
          <Callout.Text>{error}</Callout.Text>
        </Callout.Root>
      )}

      <LogHistogram buckets={buckets} interval={interval} from={range.from} to={range.to} onPick={(from, to) => setCustom({ from, to })} />
      {sampled && (
        <Text size="1" color="amber">
          This range holds a very large number of events, so the chart and counts cover only the newest 50,000. Narrow the range for exact numbers.
        </Text>
      )}

      {facets && facets.signatures.length > 0 && (
        <details className="kinds">
          <summary>
            <Text size="1" color="gray" className="mono">
              TOP EVENT KINDS ({facets.signatures.length}), click one to filter
            </Text>
          </summary>
          <Flex gap="2" wrap="wrap" mt="2">
            {facets.signatures.map((s) => (
              <button key={s.value} type="button" className="suggest kind" title={s.template ?? s.value} onClick={() => setSignature({ id: s.value, label: s.template ?? s.value })}>
                <span className="mono">{s.count.toLocaleString()}×</span> {s.template ?? s.value}
              </button>
            ))}
          </Flex>
        </details>
      )}

      <Box className="log-scroll">
        <Table.Root size="1" variant="surface" className="log-table">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeaderCell>Time</Table.ColumnHeaderCell>
              <Table.ColumnHeaderCell>Level</Table.ColumnHeaderCell>
              {showSource && <Table.ColumnHeaderCell>Source</Table.ColumnHeaderCell>}
              {showWho && <Table.ColumnHeaderCell>Actor · action</Table.ColumnHeaderCell>}
              <Table.ColumnHeaderCell width="100%">Message</Table.ColumnHeaderCell>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {items.map((e) => (
              <Table.Row
                key={e.id}
                className="log-row"
                tabIndex={0}
                aria-label={`${e.level ?? 'Log'} event${e.occurred_at ? ' at ' + new Date(e.occurred_at).toLocaleString() : ''}, open details`}
                onClick={() => setSelected(e.id)}
                onKeyDown={(k) => {
                  if (k.key === 'Enter' || k.key === ' ') {
                    k.preventDefault()
                    setSelected(e.id)
                  }
                }}
              >
                <Table.Cell className="mono c-time" style={{ whiteSpace: 'nowrap' }}>
                  {e.occurred_at ? new Date(e.occurred_at).toLocaleString() : '—'}
                </Table.Cell>
                <Table.Cell className="c-level">
                  {e.level ? (
                    <Code size="1" variant="ghost" style={{ color: levelColor(e.level.toUpperCase()) }}>
                      {e.level}
                    </Code>
                  ) : (
                    '—'
                  )}
                </Table.Cell>
                {showSource && <Table.Cell className="mono">{e.source}</Table.Cell>}
                {showWho && <Table.Cell className="mono c-who">{[e.actor, e.action].filter(Boolean).join(' · ')}</Table.Cell>}
                <Table.Cell className="mono c-msg" style={{ wordBreak: 'break-word' }}>
                  {oneLine(e.text)}
                  {(e.text.includes('\n') || e.truncated) && <Text color="gray"> …</Text>}
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
      </Box>

      {!loading && items.length === 0 && !error && <Notice>No log lines match these filters in this range.</Notice>}
      <Flex justify="center" align="center" gap="3" pb="4">
        <Text size="1" color="gray">
          {items.length.toLocaleString()} shown{total > items.length ? ` of ${total.toLocaleString()}${sampled ? '+' : ''}` : ''}
        </Text>
        {cursor && (
          <Button size="1" variant="soft" loading={loading} onClick={loadMore}>
            Load more
          </Button>
        )}
      </Flex>

      {projectId && <LogDetail projectId={projectId} eventId={selected} onClose={() => setSelected(null)} />}
    </Flex>
  )
}

function FilterBadge({ children, onClear }: { children: ReactNode; onClear: () => void }) {
  return (
    <Badge variant="soft" size="2">
      {children}
      <IconButton size="1" variant="ghost" color="gray" aria-label="Remove filter" onClick={onClear} style={{ margin: 0 }}>
        <Cross2Icon />
      </IconButton>
    </Badge>
  )
}

function Notice({ children }: { children: string }) {
  return (
    <Text as="p" size="2" color="gray" mt="6" align="center">
      {children}
    </Text>
  )
}
