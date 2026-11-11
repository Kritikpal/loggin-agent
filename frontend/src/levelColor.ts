const LEVEL_COLOR: Record<string, string> = {
  ERROR: 'var(--red-9)',
  FATAL: 'var(--red-11)',
  WARN: 'var(--amber-9)',
  WARNING: 'var(--amber-9)',
  INFO: 'var(--jade-9)',
  DEBUG: 'var(--gray-8)',
  TRACE: 'var(--gray-6)',
  UNKNOWN: 'var(--gray-7)',
}
export const LEVEL_ORDER = ['FATAL', 'ERROR', 'WARN', 'WARNING', 'INFO', 'DEBUG', 'TRACE', 'UNKNOWN']
export const colorOf = (level: string) => LEVEL_COLOR[level] ?? 'var(--blue-9)'


export const levelColor = colorOf
