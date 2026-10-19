export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return new Intl.NumberFormat('zh-CN').format(value)
}

export function formatPercent(
  value: number | null | undefined,
  digits = 0,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

export function formatBytes(value: number): string {
  if (!value) return '0 B'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

export function formatDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds < 0) return '—'
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`
}

export function formatScore(value: unknown): string {
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(1)
  if (typeof value === 'string' && value.trim()) return value
  return '—'
}

export function roleLabel(role: string): string {
  if (role === 'admin') return '行政'
  if (role === 'commercial') return '商务'
  if (role === 'technical') return '技术'
  return role || '综合'
}

export function severityLabel(severity: string): string {
  if (severity === 'high') return '高'
  if (severity === 'medium') return '中'
  if (severity === 'low') return '低'
  return severity || '提示'
}

export function severityClass(severity: string): string {
  if (severity === 'high') return 'danger'
  if (severity === 'medium') return 'warning'
  return 'info'
}

export function stageLabel(stage: string): string {
  const labels: Record<string, string> = {
    queued: '排队中',
    parse: '解析中',
    extract: '抽取中',
    ingest: '入库中',
    write: '编写中',
    done: '已完成',
  }
  return labels[stage] || stage
}

export function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: '排队中',
    running: '执行中',
    succeeded: '已完成',
    failed: '失败',
  }
  return labels[status] || status
}

export function shortDocId(docId: string, limit = 22): string {
  return docId.length > limit ? `${docId.slice(0, limit)}…` : docId
}
