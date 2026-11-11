import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { Interval, StatsBucket } from './api'
import { LEVEL_ORDER, colorOf } from './levelColor'

const STEP_MS: Record<Interval, number> = { '1m': 60_000, '5m': 300_000, '1h': 3_600_000, '1d': 86_400_000 }
const MAX_FILLED = 1000 // the API's bucket limit, so filling never makes the chart bigger than the server allows

interface Props {
  buckets: StatsBucket[]
  interval: Interval
  /** The window shown: empty buckets inside it are drawn as gaps, so a quiet day is visible, not hidden. */
  from: string
  to: string
  /** A bar column was clicked: narrow the view to that bucket. */
  onPick: (fromIso: string, toIso: string) => void
}

type Row = { ts: string; label: string; total: number } & Record<string, number | string>

/** Events per time bucket, stacked by level. The API only returns buckets that hold events; this adds the empty ones. */
export function LogHistogram({ buckets, interval, from, to, onPick }: Props) {
  const step = STEP_MS[interval]
  const levels = [...new Set(buckets.flatMap((b) => Object.keys(b.by_level)))].sort(
    (a, b) => (LEVEL_ORDER.indexOf(a) + 1 || 99) - (LEVEL_ORDER.indexOf(b) + 1 || 99),
  )
  const fmt = new Intl.DateTimeFormat(undefined, interval === '1d' ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  const byStart = new Map(buckets.map((b) => [new Date(b.start).getTime(), b]))
  const first = new Date(from).getTime()
  const count = Math.ceil((new Date(to).getTime() - first) / step)
  const starts = count > 0 && count <= MAX_FILLED ? Array.from({ length: count }, (_, i) => first + i * step) : [...byStart.keys()].sort((a, b) => a - b)
  const data: Row[] = starts.map((t) => {
    const b = byStart.get(t)
    return { ts: new Date(t).toISOString(), label: fmt.format(new Date(t)), total: b?.total ?? 0, ...(b?.by_level ?? {}) }
  })
  if (buckets.length === 0) return null

  return (
    <div style={{ height: 150, cursor: 'pointer' }} role="img" aria-label="Events over time, by level. Click a column to zoom in.">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={data}
          margin={{ top: 4, right: 8, bottom: 0, left: -16 }}
          onClick={(state) => {
            const row = data[Number(state?.activeTooltipIndex)]
            if (row) onPick(row.ts, new Date(new Date(row.ts).getTime() + step).toISOString())
          }}
        >
          <CartesianGrid vertical={false} stroke="var(--gray-a4)" />
          <XAxis dataKey="label" tick={{ fontSize: 11, fill: 'var(--gray-11)' }} minTickGap={32} tickLine={false} axisLine={false} />
          <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: 'var(--gray-11)' }} tickLine={false} axisLine={false} />
          <Tooltip
            cursor={{ fill: 'var(--gray-a3)' }}
            contentStyle={{ background: 'var(--color-panel-solid)', border: '1px solid var(--gray-a6)', borderRadius: 6, fontSize: 12 }}
            formatter={(value, name) => [value, name]}
            labelFormatter={(label, payload) => `${label} · ${payload?.[0]?.payload?.total ?? 0} events`}
          />
          {levels.map((level) => (
            <Bar key={level} dataKey={level} stackId="events" fill={colorOf(level)} isAnimationActive={false} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
