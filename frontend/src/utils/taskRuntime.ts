const LOCAL_TASK_TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})[_ T](\d{2})[:-](\d{2})[:-](\d{2})$/

export function parseTaskTimestampMillis(value: string | number | null | undefined): number | null {
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || value <= 0) return null
    return value < 10_000_000_000 ? value * 1000 : value
  }

  const text = String(value ?? '').trim()
  if (!text) return null
  if (/^\d+(?:\.\d+)?$/.test(text)) {
    return parseTaskTimestampMillis(Number(text))
  }

  const local = LOCAL_TASK_TIMESTAMP.exec(text)
  if (local) {
    const parts = local.slice(1).map(Number)
    const parsed = new Date(parts[0], parts[1] - 1, parts[2], parts[3], parts[4], parts[5])
    if (
      parsed.getFullYear() === parts[0]
      && parsed.getMonth() === parts[1] - 1
      && parsed.getDate() === parts[2]
      && parsed.getHours() === parts[3]
      && parsed.getMinutes() === parts[4]
      && parsed.getSeconds() === parts[5]
    ) {
      return parsed.getTime()
    }
    return null
  }

  const parsed = Date.parse(text)
  return Number.isFinite(parsed) ? parsed : null
}

export function formatElapsedDuration(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds)) return '(none)'
  const total = Math.max(0, Math.floor(totalSeconds))
  const days = Math.floor(total / 86_400)
  const hours = Math.floor((total % 86_400) / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  const clock = [hours, minutes, seconds].map(value => String(value).padStart(2, '0')).join(':')
  return days > 0 ? `${days}d ${clock}` : clock
}

export function formatStoredCommand(value: string | string[] | null | undefined): string {
  if (Array.isArray(value)) {
    return value.length > 0 ? JSON.stringify(value, null, 2) : ''
  }
  return String(value ?? '').trim()
}
