// Sky Viewer clock formatting (T2.1): `YYYY-MM-DD | HH : MM : SS`, UTC or local.
export type ClockMode = 'utc' | 'local'

export interface ClockParts {
  date: string
  hh: string
  mm: string
  ss: string
}

function parts(ms: number, timeZone: string | undefined): ClockParts {
  const f = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  })
  const m: Record<string, string> = {}
  for (const p of f.formatToParts(new Date(ms))) m[p.type] = p.value
  return { date: `${m.year}-${m.month}-${m.day}`, hh: m.hour!, mm: m.minute!, ss: m.second! }
}

/** `siteTimezone` = snapshot site.timezone (IANA) or null/undefined for the PC zone. */
export function formatClock(ms: number, mode: ClockMode, siteTimezone?: string | null): ClockParts {
  if (mode === 'utc') return parts(ms, 'UTC')
  try {
    return parts(ms, siteTimezone || undefined)
  }
  catch {
    return parts(ms, undefined) // unknown zone name: fall back to the PC zone
  }
}
