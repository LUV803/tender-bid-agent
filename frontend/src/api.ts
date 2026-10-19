export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed'
export type JobStage = 'queued' | 'parse' | 'extract' | 'ingest' | 'write' | 'done'
export type JobKind = 'parse' | 'bid'
export type ParserName = 'auto' | 'cloud' | 'local'

export interface JobAccepted {
  job_id: string
  status: JobStatus
  stage: JobStage
  doc_id: string
  status_url: string
}

export interface JobOptions {
  parser?: ParserName
  extract?: boolean
  ingest?: boolean
  max_sections?: number
  max_revisions?: number
}

export interface JobResponse {
  job_id: string
  status: JobStatus
  stage: JobStage
  kind: JobKind
  filename: string
  doc_id: string
  options: JobOptions
  progress: string[]
  result: Record<string, unknown>
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface BidArtifact {
  key: string
  name: string
  label: string
  size: number
  previewable: boolean
  content_url: string
  download_url: string
}

export interface BidStats {
  sections?: number
  draft_chars?: number
  document_chars?: number
  pending_markers?: number
  issues?: number
  high_issues?: number
  medium_issues?: number
  low_issues?: number
  revisions?: number
  [key: string]: number | undefined
}

export interface BidSummary {
  doc_id: string
  project_name: string
  bidder: string
  created_at: string
  elapsed_seconds: number
  stats: BidStats
  settings: Record<string, unknown>
  artifacts: BidArtifact[]
  folder: string
  warnings: string[]
}

export interface DocumentSummary {
  doc_id: string
  source_name: string
  project_name: string
  doc_type: string
  pages: number
  parser: string
  parsed_at: string
  section_count: number
  toc_count: number
  warning_count: number
  has_extraction: boolean
  extraction_partial: boolean
  ungrounded_count: number
  evaluation_factor_count: number
  has_index: boolean
  bid: BidSummary | null
}

export interface CoverageCategory {
  category: string
  factors: number
  covered: number
  max_score: number
}

export interface Coverage {
  total_factors: number
  covered_factors: number
  factor_coverage: number | null
  max_score: number
  covered_max_score: number
  score_coverage: number | null
  by_category: CoverageCategory[]
  unmapped_factors: Array<{
    factor: string
    category: string
    max_score: unknown
  }>
}

export interface SectionReport {
  id: string
  title: string
  role: string
  origin: string
  word_target: number
  factor_count: number
  factor_score: number
  factors: string[]
  issue_count: number
  high_issues: number
  medium_issues: number
  low_issues: number
}

export interface ReviewIssue {
  severity: string
  category: string
  section_id: string
  problem: string
  fix: string
  tender_basis: string
}

export interface ResponseMatrixRow {
  factor: string
  category?: string
  max_score?: number | string | null
  criteria?: string
  section_ids?: string[]
  section_titles?: string[]
  display?: string
}

export interface BidDetail extends BidSummary {
  review_summary: string
  issues: ReviewIssue[]
  unverified_claims: Array<Record<string, unknown>>
  missing_requirements: Array<Record<string, unknown>>
  coverage: Coverage
  sections: SectionReport[]
  response_matrix: ResponseMatrixRow[]
  pending_markers: Array<{ marker: string; sections?: string[] }>
  pending_count: number
  profile_gaps: Array<Record<string, unknown>>
  profile_gap_count: number
  revisions: Array<Record<string, unknown>>
}

export interface HealthInfo {
  status: string
  service: string
  version: string
  backend: string
  storage?: string
  database?: string
}

export interface BidJobRequest {
  doc_id: string
  profile: Record<string, unknown>
  company_id?: string | null
  max_sections: number
  max_revisions: number
}

export interface CompanySummary {
  name: string
  unified_social_credit_code: string
  contact_person: string
  phone: string
  qualification_count: number
  project_count: number
  personnel_count: number
}

export interface CompanyRecord {
  id: string
  name: string
  profile: Record<string, unknown>
  summary: CompanySummary
  created_at: string
  updated_at: string
}

const configuredBase = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.trim()

export const API_BASE = configuredBase
  ? configuredBase.replace(/\/+$/, '')
  : window.location.protocol === 'file:'
    ? 'http://127.0.0.1:8001'
    : ''

export function apiUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path
  return `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function errorDetail(body: unknown, fallback: string): string {
  if (typeof body === 'string' && body.trim()) return body.trim()
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) =>
          item && typeof item === 'object' && 'msg' in item
            ? String((item as { msg: unknown }).msg)
            : '',
        )
        .filter(Boolean)
      if (messages.length) return messages.join('；')
    }
  }
  return fallback
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(apiUrl(path), init)
  } catch {
    throw new ApiError(0, '无法连接后端服务，请确认 API 已在 8001 端口启动')
  }
  if (!response.ok) {
    let body: unknown = null
    try {
      body = await response.json()
    } catch {
      try {
        body = await response.text()
      } catch {
        body = null
      }
    }
    throw new ApiError(
      response.status,
      errorDetail(body, `请求失败（HTTP ${response.status}）`),
    )
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export function createParseJob(
  file: File,
  options: { parser: ParserName; extract: boolean; ingest: boolean },
): Promise<JobAccepted> {
  const form = new FormData()
  form.append('file', file)
  const params = new URLSearchParams({
    parser: options.parser,
    extract: String(options.extract),
    ingest: String(options.ingest),
  })
  return request<JobAccepted>(`/api/parse?${params.toString()}`, {
    method: 'POST',
    body: form,
  })
}

export function createBidJob(body: BidJobRequest): Promise<JobAccepted> {
  return request<JobAccepted>('/api/bids', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function getJob(jobId: string): Promise<JobResponse> {
  return request<JobResponse>(`/api/jobs/${encodeURIComponent(jobId)}`)
}

export function getHealth(): Promise<HealthInfo> {
  return request<HealthInfo>('/api/health')
}

export function getDocuments(): Promise<DocumentSummary[]> {
  return request<DocumentSummary[]>('/api/documents')
}

export function getBidHistory(): Promise<BidSummary[]> {
  return request<BidSummary[]>('/api/bids')
}

export function getBidDetail(docId: string): Promise<BidDetail> {
  return request<BidDetail>(`/api/bids/${encodeURIComponent(docId)}`)
}

export function getCompanies(): Promise<CompanyRecord[]> {
  return request<CompanyRecord[]>('/api/companies')
}

export function createCompany(
  profile: Record<string, unknown>,
): Promise<CompanyRecord> {
  return request<CompanyRecord>('/api/companies', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ profile }),
  })
}

export function updateCompany(
  companyId: string,
  profile: Record<string, unknown>,
): Promise<CompanyRecord> {
  return request<CompanyRecord>(`/api/companies/${encodeURIComponent(companyId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ profile }),
  })
}

export function deleteCompany(companyId: string): Promise<void> {
  return request<void>(`/api/companies/${encodeURIComponent(companyId)}`, {
    method: 'DELETE',
  })
}

export async function getArtifactText(path: string): Promise<string> {
  let response: Response
  try {
    response = await fetch(apiUrl(path))
  } catch {
    throw new ApiError(0, '无法连接后端服务，请确认 API 已在 8001 端口启动')
  }
  if (!response.ok) {
    throw new ApiError(response.status, `读取文件失败（HTTP ${response.status}）`)
  }
  return response.text()
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  return String(error)
}
