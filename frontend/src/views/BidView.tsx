import { useCallback, useEffect, useState } from 'react'
import type { BidJobRequest, JobAccepted } from '../api'
import {
  createBidJob,
  createCompany,
  errorMessage,
  getBidDetail,
  getCompanies,
  getDocuments,
  updateCompany,
} from '../api'
import type { Profile } from '../profile'
import {
  COMPANY_FIELDS,
  CREDIT_FIELDS,
  QUALIFICATION_FIELDS,
  SAFETY_FIELDS,
  addQualification,
  asRecord,
  asRecordList,
  asText,
  emptyProfile,
  mergeProfileTemplate,
  parseProfileJson,
  profileStats,
  profileToJson,
  removeQualification,
  withCompanyField,
  withNestedField,
  withQualificationField,
} from '../profile'
import { formatDate, formatNumber, formatScore, stageLabel, statusLabel } from '../format'
import { isTerminalJob, useApiData, useJobPolling } from '../hooks'
import { Icon } from '../icons'
import { BidReport } from '../components/BidReport'
import type { ProcessStep } from '../components/ui'
import {
  Badge,
  EmptyState,
  ErrorBanner,
  Notice,
  ProcessSteps,
  ProgressLog,
  SectionTitle,
  Spinner,
  ViewHeader,
} from '../components/ui'

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = 'text',
  wide = false,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
  type?: string
  wide?: boolean
}) {
  return (
    <label className={`field ${wide ? 'field-wide' : ''}`}>
      <span>{label}</span>
      <input
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder ?? `请输入${label}`}
        type={type}
        value={value}
      />
    </label>
  )
}

function BidReportPane({
  docId,
  refreshToken,
}: {
  docId: string
  refreshToken: string
}) {
  const load = useCallback(() => getBidDetail(docId), [docId])
  const { data, loading, error, refresh } = useApiData(load, refreshToken)

  if (!data && loading) {
    return (
      <section className="panel">
        <Spinner label="正在读取投标产物" />
      </section>
    )
  }
  if (!data && error) {
    return (
      <section className="panel">
        <ErrorBanner message={error} onRetry={refresh} />
      </section>
    )
  }
  if (!data) return null

  return (
    <div className="report-block">
      <div className="report-toolbar">
        <span>最新产物详情</span>
        <button
          className="btn btn-ghost btn-sm"
          disabled={loading}
          onClick={refresh}
          type="button"
        >
          <Icon name="refresh" size={15} />
          刷新产物
        </button>
      </div>
      <BidReport detail={data} />
    </div>
  )
}

export function BidView({
  selectedDocId,
  onSelectDoc,
  onGoUpload,
}: {
  selectedDocId: string | null
  onSelectDoc: (docId: string) => void
  onGoUpload: () => void
}) {
  const documentsRequest = useApiData(getDocuments)
  const refreshDocuments = documentsRequest.refresh
  const documents = documentsRequest.data ?? []
  const selected = documents.find((doc) => doc.doc_id === selectedDocId) ?? null

  const companiesRequest = useApiData(getCompanies)
  const companies = companiesRequest.data ?? []
  const [companyId, setCompanyId] = useState('')
  const [companyBusy, setCompanyBusy] = useState(false)
  const [companyMessage, setCompanyMessage] = useState<string | null>(null)
  const [companyError, setCompanyError] = useState<string | null>(null)

  const [profile, setProfile] = useState<Profile>(() => emptyProfile())
  const [jsonText, setJsonText] = useState(() => profileToJson(emptyProfile()))
  const [jsonError, setJsonError] = useState<string | null>(null)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [maxSections, setMaxSections] = useState(0)
  const [maxRevisions, setMaxRevisions] = useState(2)
  const [accepted, setAccepted] = useState<JobAccepted | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const { job, error: pollError } = useJobPolling(accepted)

  const busy = accepted !== null && !isTerminalJob(job)
  const jsonDirty = jsonError !== null
  const stats = profileStats(profile)
  const completeness = stats.total ? stats.filled / stats.total : 0
  const company = asRecord(profile.company)
  const credit = asRecord(profile.credit)
  const safety = asRecord(profile.safety)
  const qualifications = asRecordList(profile.qualifications)

  useEffect(() => {
    if (!documentsRequest.data) return
    if (
      selectedDocId &&
      documentsRequest.data.some((doc) => doc.doc_id === selectedDocId)
    ) {
      return
    }
    const first = documentsRequest.data.find((doc) => doc.has_extraction)
    if (first) onSelectDoc(first.doc_id)
  }, [documentsRequest.data, selectedDocId, onSelectDoc])

  useEffect(() => {
    if (job?.status === 'succeeded') refreshDocuments()
  }, [job?.status, job?.finished_at, refreshDocuments])

  const bidResult = asRecord(job?.result.bid)
  const bidStats = asRecord(bidResult.stats)
  const canStart =
    Boolean(selected?.has_extraction) &&
    Boolean(selectedDocId) &&
    !busy &&
    !submitting &&
    !jsonDirty

  const writeStep: ProcessStep = {
    key: 'write',
    label: '编写投标文件',
    detail: '分析 → 行政 / 商务 / 技术编写 → 审查修订',
    state: !accepted
      ? 'pending'
      : job?.status === 'failed'
        ? 'error'
        : job?.status === 'succeeded'
          ? 'done'
          : 'active',
  }

  function applyProfile(next: Profile) {
    setProfile(next)
    setJsonText(profileToJson(next))
    setJsonError(null)
  }

  function handleJsonChange(value: string) {
    setJsonText(value)
    const result = parseProfileJson(value)
    if (result.error !== null) {
      setJsonError(result.error)
    } else {
      setProfile(result.profile)
      setJsonError(null)
    }
  }

  function selectDocument(docId: string) {
    setAccepted(null)
    setSubmitError(null)
    onSelectDoc(docId)
  }

  function loadCompanyProfile() {
    const record = companies.find((item) => item.id === companyId)
    if (!record) {
      setCompanyError('请先选择一家公司')
      return
    }
    applyProfile(mergeProfileTemplate(record.profile))
    setCompanyMessage(`已载入「${record.name}」，可在表单中继续修改本次资料`)
    setCompanyError(null)
  }

  async function saveCompanyProfile() {
    if (companyBusy) return
    setCompanyBusy(true)
    setCompanyError(null)
    setCompanyMessage(null)
    try {
      const existing = companies.find((item) => item.id === companyId)
      const record = existing
        ? await updateCompany(existing.id, profile)
        : await createCompany(profile)
      setCompanyId(record.id)
      setCompanyMessage(
        existing
          ? `已更新公司资料「${record.name}」`
          : `已保存到 PostgreSQL：「${record.name}」`,
      )
      companiesRequest.refresh()
    } catch (reason) {
      setCompanyError(errorMessage(reason))
    } finally {
      setCompanyBusy(false)
    }
  }

  async function startBid() {
    if (!selectedDocId || !canStart) return
    setSubmitting(true)
    setSubmitError(null)
    const body: BidJobRequest = {
      doc_id: selectedDocId,
      profile,
      company_id: companyId || null,
      max_sections: maxSections,
      max_revisions: maxRevisions,
    }
    try {
      const response = await createBidJob(body)
      setAccepted(response)
    } catch (reason) {
      setSubmitError(errorMessage(reason))
    } finally {
      setSubmitting(false)
    }
  }

  const showReport = Boolean(
    selectedDocId &&
      !busy &&
      (selected?.bid || job?.status === 'succeeded'),
  )

  return (
    <div>
      <ViewHeader
        actions={
          <div className="header-badges">
            <Badge tone="accent" dot>
              多 Agent
            </Badge>
            <Badge tone="neutral">固定 11 章</Badge>
          </div>
        }
        description="填写投标人资料并设置编写参数，系统会基于招标文件撰写标书，并输出来源可追溯的审查报告。"
        eyebrow="Step 02 · Write"
        title="投标文件编写"
      />

      {documentsRequest.error && !documentsRequest.data ? (
        <section className="panel">
          <ErrorBanner
            message={documentsRequest.error}
            onRetry={documentsRequest.refresh}
          />
        </section>
      ) : null}

      {!documentsRequest.data && documentsRequest.loading ? (
        <section className="panel">
          <Spinner label="正在读取已解析文档" />
        </section>
      ) : null}

      {documentsRequest.data && documents.length === 0 ? (
        <section className="panel">
          <EmptyState
            action={
              <button className="btn btn-primary" onClick={onGoUpload} type="button">
                上传招标文件
                <Icon name="arrow-right" size={17} />
              </button>
            }
            description="先上传一份 PDF 并完成解析与结构化抽取，之后才能发起标书编写。"
            icon="library"
            title="文档库还是空的"
          />
        </section>
      ) : null}

      {documents.length ? (
        <>
          <section className="panel doc-panel">
            <SectionTitle
              aside={
                <button
                  className="btn btn-ghost btn-sm"
                  disabled={documentsRequest.loading}
                  onClick={documentsRequest.refresh}
                  type="button"
                >
                  <Icon name="refresh" size={15} />
                  刷新文档
                </button>
              }
              description="选择一份已完成结构化抽取的文档作为招标依据。"
              title="编写对象"
            />
            <div className="doc-select-row">
              <label className="select-field">
                <span>招标文档</span>
                <select
                  disabled={busy}
                  onChange={(event) => selectDocument(event.target.value)}
                  value={selectedDocId ?? ''}
                >
                  <option disabled value="">
                    请选择文档
                  </option>
                  {selectedDocId &&
                  !documents.some((doc) => doc.doc_id === selectedDocId) ? (
                    <option value={selectedDocId}>{selectedDocId}</option>
                  ) : null}
                  {documents.map((doc) => (
                    <option
                      disabled={!doc.has_extraction}
                      key={doc.doc_id}
                      value={doc.doc_id}
                    >
                      {doc.project_name || doc.source_name || doc.doc_id}
                      {doc.has_extraction ? '' : ' · 未抽取'}
                    </option>
                  ))}
                </select>
              </label>
              {selected ? (
                <div className="doc-meta">
                  <span>{formatNumber(selected.pages)} 页</span>
                  <span>{formatNumber(selected.section_count)} 个章节</span>
                  <span>解析于 {formatDate(selected.parsed_at)}</span>
                  <Badge tone={selected.has_extraction ? 'success' : 'warning'}>
                    {selected.has_extraction ? '已抽取' : '未抽取'}
                  </Badge>
                  {selected.has_index ? <Badge tone="info">已入库</Badge> : null}
                  {selected.bid ? <Badge tone="accent">已有产物</Badge> : null}
                </div>
              ) : null}
            </div>
            {selectedDocId ? (
              <small className="doc-id-line">
                doc_id <code className="inline-code">{selectedDocId}</code>
                {selected?.source_name ? <span>{selected.source_name}</span> : null}
              </small>
            ) : null}
            {selected && !selected.has_extraction ? (
              <Notice tone="warning" title="该文档尚未完成结构化抽取">
                编写前需要执行抽取，例如：
                <code>uv run python -m bidding extract {selected.doc_id}</code>
                完成后刷新文档列表。
              </Notice>
            ) : null}
          </section>

          <div className="bid-layout">
            <section className="panel">
              <SectionTitle
                aside={
                  <Badge tone={stats.filled ? 'accent' : 'neutral'}>
                    已填 {stats.filled} / {stats.total} 个字段
                  </Badge>
                }
                description="常用资料用表单填写；复杂结构（业绩、人员、财务等）可展开高级 JSON 编辑。"
                title="投标人资料"
              />
              <div className="company-loader">
                <div className="company-loader-head">
                  <span className="company-loader-icon">
                    <Icon name="database" size={17} />
                  </span>
                  <div>
                    <strong>公司资料库</strong>
                    <small>
                      从 PostgreSQL 载入常用资料，或把当前表单保存为新公司。
                    </small>
                  </div>
                </div>
                <div className="company-loader-controls">
                  <select
                    disabled={companyBusy || companiesRequest.loading}
                    onChange={(event) => {
                      setCompanyId(event.target.value)
                      setCompanyMessage(null)
                      setCompanyError(null)
                    }}
                    value={companyId}
                  >
                    <option value="">未选择已保存公司</option>
                    {companies.map((record) => (
                      <option key={record.id} value={record.id}>
                        {record.name}
                      </option>
                    ))}
                  </select>
                  <button
                    className="btn btn-ghost btn-sm"
                    disabled={!companyId || companyBusy}
                    onClick={loadCompanyProfile}
                    type="button"
                  >
                    载入资料
                  </button>
                  <button
                    className="btn btn-secondary btn-sm"
                    disabled={companyBusy || jsonDirty}
                    onClick={() => {
                      void saveCompanyProfile()
                    }}
                    type="button"
                  >
                    {companyBusy ? <Spinner /> : <Icon name="database" size={15} />}
                    {companyId ? '更新公司资料' : '保存为新公司'}
                  </button>
                </div>
                {companiesRequest.error ? (
                  <p className="company-hint warning">
                    公司资料库不可用，仍可手动填写资料：{companiesRequest.error}
                  </p>
                ) : null}
                {companyMessage ? (
                  <p className="company-hint success">
                    <Icon name="check" size={14} />
                    {companyMessage}
                  </p>
                ) : null}
                {companyError ? <ErrorBanner message={companyError} /> : null}
              </div>
              <div className="completeness">
                <span style={{ width: `${Math.round(completeness * 100)}%` }} />
              </div>
              {stats.filled === 0 ? (
                <Notice tone="info">
                  没有资料也能生成草稿，但所有投标人事实都会显示为【待填写】占位符；
                  系统不会编造公司名称、资质、业绩或人员信息。
                </Notice>
              ) : null}

              <div className="form-section">
                <h3>公司基本信息</h3>
                <div className="form-grid">
                  {COMPANY_FIELDS.map(([key, label]) => (
                    <Field
                      key={key}
                      label={label}
                      onChange={(value) =>
                        applyProfile(withCompanyField(profile, key, value))
                      }
                      placeholder={
                        key === 'name'
                          ? '请输入公司全称'
                          : key === 'established_date' ||
                              key === 'registered_capital'
                            ? '如：2010-05-01 / 5000 万元'
                            : undefined
                      }
                      type={
                        key === 'email' ? 'email' : key === 'phone' ? 'tel' : 'text'
                      }
                      value={asText(company[key])}
                      wide={key === 'address'}
                    />
                  ))}
                </div>
              </div>

              <div className="form-section">
                <div className="form-section-head">
                  <h3>资质证书</h3>
                  <button
                    className="btn btn-ghost btn-sm"
                    onClick={() => applyProfile(addQualification(profile))}
                    type="button"
                  >
                    <Icon name="sparkles" size={15} />
                    添加资质
                  </button>
                </div>
                {qualifications.length ? (
                  <div className="repeat-list">
                    {qualifications.map((item, index) => (
                      <div className="repeat-card" key={index}>
                        <div className="repeat-head">
                          <strong>资质 {index + 1}</strong>
                          <button
                            aria-label={`移除资质 ${index + 1}`}
                            className="icon-button"
                            onClick={() =>
                              applyProfile(removeQualification(profile, index))
                            }
                            type="button"
                          >
                            <Icon name="x" size={16} />
                          </button>
                        </div>
                        <div className="form-grid">
                          {QUALIFICATION_FIELDS.map(([key, label]) => (
                            <Field
                              key={key}
                              label={label}
                              onChange={(value) =>
                                applyProfile(
                                  withQualificationField(
                                    profile,
                                    index,
                                    key,
                                    value,
                                  ),
                                )
                              }
                              value={asText(item[key])}
                            />
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="muted">
                    尚未添加资质。留空时标书会保留【待填写：资质】占位符。
                  </p>
                )}
              </div>

              <div className="form-section">
                <h3>信用与安全</h3>
                <div className="form-grid form-grid-two">
                  {CREDIT_FIELDS.map(([key, label]) => (
                    <Field
                      key={`credit-${key}`}
                      label={label}
                      onChange={(value) =>
                        applyProfile(withNestedField(profile, 'credit', key, value))
                      }
                      value={asText(credit[key])}
                    />
                  ))}
                  {SAFETY_FIELDS.map(([key, label]) => (
                    <Field
                      key={`safety-${key}`}
                      label={label}
                      onChange={(value) =>
                        applyProfile(withNestedField(profile, 'safety', key, value))
                      }
                      value={asText(safety[key])}
                    />
                  ))}
                </div>
              </div>

              <details
                className="advanced-json"
                onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
                open={advancedOpen}
              >
                <summary>
                  <span className="summary-title">
                    <Icon name="braces" size={18} />
                    高级 JSON
                  </span>
                  <span className="summary-hint">
                    类似业绩、人员、财务、管理体系等结构
                    <Icon name="chevron-down" size={16} />
                  </span>
                </summary>
                <div className="advanced-json-body">
                  <p className="json-help">
                    表单修改会实时写入 JSON；在下方编辑合法 JSON 对象后也会同步回表单。
                    空字段保留【待填写】占位符，不会被模型编造。
                  </p>
                  <textarea
                    aria-label="投标人资料 JSON"
                    className="json-editor"
                    onChange={(event) => handleJsonChange(event.target.value)}
                    spellCheck={false}
                    value={jsonText}
                  />
                  {jsonError ? (
                    <ErrorBanner message={jsonError} />
                  ) : (
                    <p className="json-synced">
                      <Icon name="check" size={15} />
                      当前 JSON 已同步到表单
                    </p>
                  )}
                  <div className="json-actions">
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={() => {
                        setJsonText(profileToJson(profile))
                        setJsonError(null)
                      }}
                      type="button"
                    >
                      重新格式化
                    </button>
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={() => applyProfile(emptyProfile())}
                      type="button"
                    >
                      清空资料
                    </button>
                  </div>
                </div>
              </details>
            </section>

            <aside className="stack">
              <section className="panel run-card bid-run-card">
                <div className="run-card-head">
                  <span className="run-icon">
                    <Icon name="pen" size={20} />
                  </span>
                  <div>
                    <h2>编写参数</h2>
                    <p>编写在 Celery worker 中执行，完成后产物写入 upload/bids。</p>
                  </div>
                </div>

                <div className="param-group">
                  <span className="field-label">新增章节上限</span>
                  <div className="segmented">
                    {[0, 1, 2, 3].map((value) => (
                      <button
                        className={maxSections === value ? 'is-active' : ''}
                        disabled={busy}
                        key={value}
                        onClick={() => setMaxSections(value)}
                        type="button"
                      >
                        {value}
                      </button>
                    ))}
                  </div>
                  <small>0 表示只保留固定 11 章；分析师最多新增 3 章。</small>
                </div>

                <label className="select-field">
                  <span>自动修订章节数</span>
                  <select
                    disabled={busy}
                    onChange={(event) => setMaxRevisions(Number(event.target.value))}
                    value={maxRevisions}
                  >
                    {Array.from({ length: 11 }, (_, value) => value).map((value) => (
                      <option key={value} value={value}>
                        {value} 个章节
                      </option>
                    ))}
                  </select>
                  <small>审查 Agent 会优先修订事实与编造类问题。</small>
                </label>

                {accepted ? (
                  <div className="job-status-line">
                    <Badge
                      dot
                      tone={
                        job?.status === 'failed'
                          ? 'danger'
                          : job?.status === 'succeeded'
                            ? 'success'
                            : 'info'
                      }
                    >
                      {job ? statusLabel(job.status) : '已提交'}
                    </Badge>
                    <span>{job ? stageLabel(job.stage) : '等待 worker'}</span>
                  </div>
                ) : null}

                <ProcessSteps steps={[writeStep]} />
                {job?.progress.length ? <ProgressLog messages={job.progress} /> : null}
                {accepted && !job && !pollError ? (
                  <Spinner label="正在获取任务状态" />
                ) : null}

                {pollError ? <ErrorBanner message={pollError} /> : null}
                {submitError ? <ErrorBanner message={submitError} /> : null}
                {job?.error ? <ErrorBanner message={job.error} /> : null}

                {job?.status === 'succeeded' && Object.keys(bidResult).length ? (
                  <Notice tone="success" title="投标文件已生成">
                    {asText(bidResult.project_name) || '项目'} ·{' '}
                    {formatScore(bidStats.sections)} 个章节 ·{' '}
                    {formatScore(bidStats.issues)} 个审查问题。完整报告见下方。
                  </Notice>
                ) : null}

                <button
                  className="btn btn-primary btn-block"
                  disabled={!canStart}
                  onClick={() => {
                    void startBid()
                  }}
                  type="button"
                >
                  {submitting ? (
                    <Spinner />
                  ) : busy ? (
                    '正在编写…'
                  ) : (
                    <>
                      <Icon name="play" size={17} />
                      开始编写
                    </>
                  )}
                </button>
                {!selectedDocId ? (
                  <p className="run-footnote">请先选择一份招标文档。</p>
                ) : jsonDirty ? (
                  <p className="run-footnote">请先修正高级 JSON 中的错误。</p>
                ) : !selected?.has_extraction ? (
                  <p className="run-footnote">该文档尚未完成结构化抽取。</p>
                ) : (
                  <p className="run-footnote">
                    缺失的投标人资料会保留【待填写】占位符。
                  </p>
                )}
              </section>
            </aside>
          </div>
        </>
      ) : null}

      {showReport && selectedDocId ? (
        <BidReportPane
          docId={selectedDocId}
          refreshToken={`${job?.finished_at ?? ''}|${selected?.bid?.created_at ?? ''}`}
        />
      ) : null}

      {selectedDocId && !busy && !showReport ? (
        <section className="panel">
          <EmptyState
            description="填写投标人资料并点击“开始编写”，评分覆盖、章节报告与审查意见会显示在这里。"
            icon="document-check"
            title="该文档还没有投标产物"
          />
        </section>
      ) : null}
    </div>
  )
}
