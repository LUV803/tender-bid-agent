import { useRef, useState } from 'react'
import type { JobAccepted, ParserName } from '../api'
import { createParseJob, errorMessage } from '../api'
import { formatBytes, formatNumber, stageLabel, statusLabel } from '../format'
import { isTerminalJob, useJobPolling } from '../hooks'
import { Icon } from '../icons'
import { asRecord, asText } from '../profile'
import {
  Badge,
  ErrorBanner,
  Metric,
  Notice,
  ProcessSteps,
  ProgressLog,
  SectionTitle,
  Spinner,
  ViewHeader,
} from '../components/ui'
import type { ProcessStep } from '../components/ui'

const PARSER_OPTIONS: Array<{
  value: ParserName
  title: string
  description: string
}> = [
  {
    value: 'auto',
    title: '智能选择',
    description: '优先 MinerU 云端解析，未配置或失败时回退 PyMuPDF。',
  },
  {
    value: 'cloud',
    title: 'MinerU 云端',
    description: '版式还原更完整，需要 MINERU_API_KEY 与网络连接。',
  },
  {
    value: 'local',
    title: 'PyMuPDF 本地',
    description: '无需外部解析服务，速度快，复杂版式的还原能力有限。',
  },
]

function arrayLength(value: unknown): number {
  return Array.isArray(value) ? value.length : 0
}

export function UploadView({ onWrite }: { onWrite: (docId: string) => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [parser, setParser] = useState<ParserName>('auto')
  const [extract, setExtract] = useState(true)
  const [ingest, setIngest] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [accepted, setAccepted] = useState<JobAccepted | null>(null)
  const [submitted, setSubmitted] = useState<{
    parser: ParserName
    extract: boolean
    ingest: boolean
  } | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const { job, error: pollError } = useJobPolling(accepted)

  const busy = accepted !== null && !isTerminalJob(job)
  const succeeded = job?.status === 'succeeded'
  const failed = job?.status === 'failed'

  const stages: ProcessStep[] = submitted
    ? [
        {
          key: 'parse',
          label: '文档解析',
          detail: `${
            PARSER_OPTIONS.find((item) => item.value === submitted.parser)?.title ??
            submitted.parser
          } · 还原章节与页码`,
          state: 'pending',
        },
        ...(submitted.extract
          ? [
              {
                key: 'extract',
                label: '结构化抽取',
                detail: '项目要素、资格条件、评分办法',
                state: 'pending' as const,
              },
            ]
          : []),
        ...(submitted.ingest
          ? [
              {
                key: 'ingest',
                label: '向量入库',
                detail: '分块、嵌入并写入 Chroma',
                state: 'pending' as const,
              },
            ]
          : []),
      ]
    : []

  const currentIndex = job ? stages.findIndex((item) => item.key === job.stage) : -1
  const completed =
    succeeded || (job !== null && job.stage === 'done' && job.status !== 'failed')
  const steps = stages.map((step, index): ProcessStep => {
    if (!accepted) return { ...step, state: 'pending' }
    if (completed) return { ...step, state: 'done' }
    if (failed) {
      if (currentIndex >= 0) {
        if (index < currentIndex) return { ...step, state: 'done' }
        if (index === currentIndex) return { ...step, state: 'error' }
      }
      return { ...step, state: 'pending' }
    }
    if (currentIndex < 0) return { ...step, state: 'pending' }
    if (index < currentIndex) return { ...step, state: 'done' }
    if (index === currentIndex) return { ...step, state: 'active' }
    return { ...step, state: 'pending' }
  })

  const parseResult = asRecord(job?.result.parse)
  const extractResult = asRecord(job?.result.extract)
  const ingestResult = asRecord(job?.result.ingest)
  const resultDocId = asText(parseResult.doc_id) || accepted?.doc_id || ''
  const hasExtraction = Object.keys(extractResult).length > 0
  const extractionMeta = asRecord(extractResult.extraction_meta)
  const ungrounded = arrayLength(extractResult.ungrounded_fields)
  const warnings = Array.isArray(parseResult.warnings)
    ? parseResult.warnings.map((item) => String(item))
    : []

  function chooseFile(next: File | undefined) {
    if (!next) return
    if (!next.name.toLowerCase().endsWith('.pdf')) {
      setFileError('仅支持 PDF 文件。')
      return
    }
    setFileError(null)
    setSubmitError(null)
    setFile(next)
    setAccepted(null)
    setSubmitted(null)
  }

  async function start() {
    if (!file || busy || submitting) return
    setSubmitting(true)
    setSubmitError(null)
    const options = { parser, extract, ingest }
    try {
      const response = await createParseJob(file, options)
      setSubmitted(options)
      setAccepted(response)
    } catch (reason) {
      setSubmitError(errorMessage(reason))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div>
      <ViewHeader
        actions={
          <div className="header-badges">
            <Badge tone="accent" dot>
              异步任务
            </Badge>
            <Badge tone="neutral">PDF</Badge>
          </div>
        }
        description="上传招标文件，解析为带页码的结构化全文；可选继续抽取项目要素并写入向量库。"
        eyebrow="Step 01 · Parse"
        title="上传与解析"
      />

      <div className="upload-grid">
        <div className="stack">
          <section className="panel">
            <SectionTitle
              description="文件会上传到本机后端 upload/tmp，任务完成后自动清理临时副本。"
              title="选择招标文件"
            />
            <input
              accept="application/pdf,.pdf"
              hidden
              onChange={(event) => {
                chooseFile(event.target.files?.[0])
                event.target.value = ''
              }}
              ref={inputRef}
              type="file"
            />
            <button
              className={`dropzone ${dragging ? 'is-dragging' : ''} ${
                file ? 'has-file' : ''
              }`}
              disabled={busy}
              onClick={() => inputRef.current?.click()}
              onDragEnter={(event) => {
                event.preventDefault()
                setDragging(true)
              }}
              onDragLeave={(event) => {
                event.preventDefault()
                setDragging(false)
              }}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault()
                setDragging(false)
                chooseFile(event.dataTransfer.files?.[0])
              }}
              type="button"
            >
              {file ? (
                <span className="file-card">
                  <span className="file-card-icon">
                    <Icon name="file" size={26} />
                  </span>
                  <span className="file-card-copy">
                    <strong>{file.name}</strong>
                    <small>{formatBytes(file.size)} · 点击更换文件</small>
                  </span>
                  <Badge tone="success">已选择</Badge>
                </span>
              ) : (
                <span className="dropzone-inner">
                  <span className="dropzone-icon">
                    <Icon name="upload" size={26} />
                  </span>
                  <strong>拖拽 PDF 到这里，或点击选择</strong>
                  <small>仅支持 PDF，上传后自动进入异步解析队列</small>
                </span>
              )}
            </button>
            {fileError ? <ErrorBanner message={fileError} /> : null}
          </section>

          <section className="panel">
            <SectionTitle
              description="解析方式决定版式还原效果；抽取与入库是相互独立的可选阶段。"
              title="解析与处理选项"
            />
            <div className="option-grid">
              {PARSER_OPTIONS.map((option) => (
                <label
                  className={`radio-card ${parser === option.value ? 'is-selected' : ''}`}
                  key={option.value}
                >
                  <input
                    checked={parser === option.value}
                    disabled={busy}
                    name="parser"
                    onChange={() => setParser(option.value)}
                    type="radio"
                    value={option.value}
                  />
                  <span className="radio-mark" />
                  <span>
                    <strong>{option.title}</strong>
                    <small>{option.description}</small>
                  </span>
                </label>
              ))}
            </div>

            <div className="toggle-grid">
              <label className={`toggle-card ${extract ? 'is-on' : ''}`}>
                <input
                  checked={extract}
                  disabled={busy}
                  onChange={(event) => setExtract(event.target.checked)}
                  type="checkbox"
                />
                <span className="switch" />
                <span>
                  <strong>结构化抽取</strong>
                  <small>
                    提取项目名称、资格条件与评分办法，编写标书前必需。
                  </small>
                </span>
              </label>
              <label className={`toggle-card ${ingest ? 'is-on' : ''}`}>
                <input
                  checked={ingest}
                  disabled={busy}
                  onChange={(event) => setIngest(event.target.checked)}
                  type="checkbox"
                />
                <span className="switch" />
                <span>
                  <strong>向量入库</strong>
                  <small>
                    分块、调用智谱 embedding，写入 Chroma；需要 Chroma 服务。
                  </small>
                </span>
              </label>
            </div>
          </section>
        </div>

        <aside className="panel run-card">
          <div className="run-card-head">
            <span className="run-icon">
              <Icon name="sparkles" size={20} />
            </span>
            <div>
              <h2>解析流水线</h2>
              <p>任务在 Celery worker 中执行，可离开页面后继续运行。</p>
            </div>
          </div>

          {accepted ? (
            <div className="job-status-line">
              <Badge
                tone={
                  failed ? 'danger' : succeeded ? 'success' : busy ? 'info' : 'neutral'
                }
                dot
              >
                {job ? statusLabel(job.status) : '已提交'}
              </Badge>
              <span>{job ? stageLabel(job.stage) : '等待 worker'}</span>
              {job?.doc_id ? <code className="inline-code">{job.doc_id}</code> : null}
            </div>
          ) : null}

          {steps.length ? (
            <ProcessSteps steps={steps} />
          ) : (
            <div className="pending-hint">
              <Icon name="clock" size={18} />
              <p>提交任务后，这里会显示解析、抽取和入库的实时阶段。</p>
            </div>
          )}

          {job?.progress.length ? (
            <ProgressLog messages={job.progress} />
          ) : accepted && !job && !pollError ? (
            <Spinner label="正在获取任务状态" />
          ) : null}

          {pollError ? <ErrorBanner message={pollError} /> : null}
          {submitError ? <ErrorBanner message={submitError} /> : null}
          {job?.error ? <ErrorBanner message={job.error} /> : null}

          <button
            className="btn btn-primary btn-block"
            disabled={!file || busy || submitting || succeeded}
            onClick={() => {
              void start()
            }}
            type="button"
          >
            {submitting ? (
              <Spinner />
            ) : busy ? (
              '任务执行中…'
            ) : succeeded ? (
              <>
                <Icon name="check" size={17} />
                解析完成
              </>
            ) : failed ? (
              <>
                <Icon name="refresh" size={17} />
                重新解析
              </>
            ) : (
              <>
                <Icon name="play" size={17} />
                开始解析
              </>
            )}
          </button>
          {!file ? <p className="run-footnote">请先选择一份 PDF 文件。</p> : null}
          {file && !busy && !succeeded ? (
            <p className="run-footnote">
              将使用 {PARSER_OPTIONS.find((item) => item.value === parser)?.title}
              {extract ? ' · 结构化抽取' : ''}
              {ingest ? ' · 向量入库' : ''}
            </p>
          ) : null}
        </aside>
      </div>

      {resultDocId ? (
        <section className="panel result-panel">
          <div className="result-head">
            <div>
              <div className="eyebrow">Parse Summary</div>
              <h2>{asText(parseResult.source_name) || file?.name || '解析摘要'}</h2>
              <p>
                <code className="inline-code">{resultDocId}</code>
                <span>解析器 {asText(parseResult.parser) || '—'}</span>
                {asText(parseResult.parsed_at) ? (
                  <span>时间 {asText(parseResult.parsed_at)}</span>
                ) : null}
              </p>
            </div>
            {hasExtraction ? (
              <button
                className="btn btn-primary"
                onClick={() => onWrite(resultDocId)}
                type="button"
              >
                前往编写
                <Icon name="arrow-right" size={17} />
              </button>
            ) : succeeded ? (
              <Badge tone="warning">未执行抽取，暂不能编写</Badge>
            ) : null}
          </div>

          <div className="metric-grid metric-grid-compact">
            <Metric
              icon="file"
              label="页数"
              value={formatNumber(Number(parseResult.pages) || 0)}
            />
            <Metric
              icon="library"
              label="章节"
              value={formatNumber(arrayLength(parseResult.sections))}
            />
            <Metric
              icon="library"
              label="目录项"
              value={formatNumber(arrayLength(parseResult.toc))}
            />
            <Metric
              icon="alert"
              label="解析警告"
              tone={warnings.length ? 'warning' : 'success'}
              value={formatNumber(warnings.length)}
            />
            <Metric
              icon="chart"
              label="评分项"
              value={
                hasExtraction
                  ? formatNumber(arrayLength(extractResult.evaluation_factors))
                  : '—'
              }
            />
            <Metric
              icon="shield"
              label="待核对字段"
              tone={ungrounded ? 'warning' : 'neutral'}
              value={hasExtraction ? formatNumber(ungrounded) : '—'}
            />
          </div>

          {hasExtraction ? (
            <div className="summary-split">
              <div>
                <h3>抽取结果</h3>
                <dl className="detail-list">
                  <div>
                    <dt>项目名称</dt>
                    <dd>{asText(extractResult.project_name) || '—'}</dd>
                  </div>
                  <div>
                    <dt>评分项</dt>
                    <dd>
                      {formatNumber(arrayLength(extractResult.evaluation_factors))}
                    </dd>
                  </div>
                  <div>
                    <dt>抽取状态</dt>
                    <dd>
                      {extractionMeta.partial ? (
                        <Badge tone="warning">部分完成</Badge>
                      ) : (
                        <Badge tone="success">完整</Badge>
                      )}
                    </dd>
                  </div>
                </dl>
              </div>
              <div>
                <h3>索引结果</h3>
                {Object.keys(ingestResult).length ? (
                  <dl className="detail-list">
                    <div>
                      <dt>集合</dt>
                      <dd>{asText(ingestResult.collection) || '—'}</dd>
                    </div>
                    <div>
                      <dt>分块</dt>
                      <dd>{formatNumber(Number(ingestResult.chunks) || 0)}</dd>
                    </div>
                    <div>
                      <dt>已存储</dt>
                      <dd>{formatNumber(Number(ingestResult.stored_chunks) || 0)}</dd>
                    </div>
                  </dl>
                ) : (
                  <p className="muted">
                    {submitted?.ingest
                      ? '本次入库未完成。'
                      : '本次未启用向量入库，编写时仍可使用抽取结果。'}
                  </p>
                )}
              </div>
            </div>
          ) : null}

          {warnings.length ? (
            <details className="report-details" open>
              <summary>解析警告（{warnings.length}）</summary>
              <ul className="compact-list">
                {warnings.map((warning, index) => (
                  <li key={index}>
                    <span>{warning}</span>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}

          {hasExtraction ? (
            <Notice tone="info">
              在编写页中，招标文件事实只来自解析与抽取产物；投标人事实只来自资料，
              缺失字段会保留【待填写】占位符。
            </Notice>
          ) : extractedNothing(submitted) ? (
            <Notice tone="warning" title="尚未结构化抽取">
              编写投标文件前需要先完成结构化抽取。请重新上传并勾选“结构化抽取”，
              或使用 CLI 执行 <code>uv run python -m bidding extract {resultDocId}</code>。
            </Notice>
          ) : null}
        </section>
      ) : null}
    </div>
  )
}

function extractedNothing(
  submitted: { extract: boolean } | null,
): boolean {
  return submitted?.extract === false
}
