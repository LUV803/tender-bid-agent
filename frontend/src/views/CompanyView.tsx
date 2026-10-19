import { useEffect, useState } from 'react'
import type { CompanyRecord } from '../api'
import {
  createCompany,
  deleteCompany,
  errorMessage,
  getCompanies,
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
import { formatDate, formatNumber } from '../format'
import { useApiData } from '../hooks'
import { Icon } from '../icons'
import {
  Badge,
  EmptyState,
  ErrorBanner,
  Notice,
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

function profileForForm(record: CompanyRecord): Profile {
  return mergeProfileTemplate(record.profile)
}

export function CompanyView() {
  const companiesRequest = useApiData(getCompanies)
  const { refresh, data, loading, error } = companiesRequest
  const companies = data ?? []

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [profile, setProfile] = useState<Profile>(() => emptyProfile())
  const [jsonText, setJsonText] = useState(() => profileToJson(emptyProfile()))
  const [jsonError, setJsonError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<string | null>(null)
  const [advancedOpen, setAdvancedOpen] = useState(false)

  const selected = companies.find((item) => item.id === selectedId) ?? null
  const stats = profileStats(profile)
  const company = asRecord(profile.company)
  const credit = asRecord(profile.credit)
  const safety = asRecord(profile.safety)
  const qualifications = asRecordList(profile.qualifications)

  useEffect(() => {
    if (selectedId && data && !data.some((item) => item.id === selectedId)) {
      setSelectedId(null)
    }
  }, [data, selectedId])

  function applyProfile(next: Profile) {
    setProfile(next)
    setJsonText(profileToJson(next))
    setJsonError(null)
    setFeedback(null)
    setSaveError(null)
  }

  function startNew() {
    setSelectedId(null)
    applyProfile(emptyProfile())
    setFeedback(null)
    setSaveError(null)
  }

  function selectCompany(record: CompanyRecord) {
    setSelectedId(record.id)
    applyProfile(profileForForm(record))
    setFeedback(null)
    setSaveError(null)
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

  async function save() {
    if (saving || deleting || jsonError) return
    setSaving(true)
    setSaveError(null)
    setFeedback(null)
    try {
      const record = selectedId
        ? await updateCompany(selectedId, profile)
        : await createCompany(profile)
      setSelectedId(record.id)
      setProfile(profileForForm(record))
      setJsonText(profileToJson(profileForForm(record)))
      setFeedback(
        selectedId
          ? `已更新「${record.name}」`
          : `已写入 PostgreSQL：「${record.name}」`,
      )
      refresh()
    } catch (reason) {
      setSaveError(errorMessage(reason))
    } finally {
      setSaving(false)
    }
  }

  async function remove() {
    if (!selectedId || saving || deleting) return
    if (!window.confirm(`确认删除「${selected?.name ?? '该'}」公司资料？此操作不可撤销。`)) {
      return
    }
    setDeleting(true)
    setSaveError(null)
    setFeedback(null)
    try {
      await deleteCompany(selectedId)
      setFeedback('公司资料已删除')
      setSelectedId(null)
      applyProfile(emptyProfile())
      refresh()
    } catch (reason) {
      setSaveError(errorMessage(reason))
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div>
      <ViewHeader
        actions={
          <div className="header-badges">
            <Badge tone="info" dot>
              PostgreSQL
            </Badge>
            <button className="btn btn-primary btn-sm" onClick={startNew} type="button">
              <Icon name="plus" size={16} />
              新建公司
            </button>
          </div>
        }
        description="公司资料持久化在 PostgreSQL，写标时可直接载入并覆盖本次填写内容；未填字段仍保留【待填写】占位符。"
        eyebrow="Step 03 · Companies"
        title="公司资料库"
      />

      {error ? (
        <ErrorBanner
          message={error}
          onRetry={refresh}
        />
      ) : null}
      {error ? (
        <Notice tone="warning" title="公司资料库暂不可用">
          请启动 PostgreSQL 服务，并在 <code className="inline-code">.env</code> 中配置可用的{' '}
          <code className="inline-code">DATABASE_URL</code>。首次保存时会自动创建{' '}
          <code className="inline-code">companies</code> 表。
        </Notice>
      ) : null}

      <div className="company-layout">
        <section className="panel company-list-panel">
          <SectionTitle
            aside={
              <button
                className="btn btn-ghost btn-sm"
                disabled={loading}
                onClick={refresh}
                type="button"
              >
                <Icon name="refresh" size={15} />
                刷新
              </button>
            }
            description="选择一家公司继续编辑，或新建一条资料。"
            title="公司列表"
          />
          {loading && !data ? <Spinner label="正在读取公司资料" /> : null}
          {data && companies.length === 0 ? (
            <EmptyState
              action={
                <button className="btn btn-secondary btn-sm" onClick={startNew} type="button">
                  <Icon name="plus" size={15} />
                  新建公司
                </button>
              }
              description="把常用投标人信息保存到 PostgreSQL，写标时可一键载入。"
              icon="database"
              title="还没有公司资料"
            />
          ) : null}
          <div className="company-list">
            {companies.map((record) => (
              <button
                className={`company-item ${record.id === selectedId ? 'is-active' : ''}`}
                key={record.id}
                onClick={() => selectCompany(record)}
                type="button"
              >
                <span className="company-item-head">
                  <strong>{record.name || '未命名公司'}</strong>
                  <span className="company-item-count">
                    {formatNumber(record.summary.qualification_count)} 项资质
                  </span>
                </span>
                <span className="company-item-meta">
                  {record.summary.unified_social_credit_code || '未填写统一社会信用代码'}
                </span>
                <span className="company-item-meta">
                  {record.summary.contact_person || '未填写联系人'}
                  {record.summary.phone ? ` · ${record.summary.phone}` : ''}
                </span>
                <span className="company-item-foot">
                  更新于 {formatDate(record.updated_at)}
                </span>
              </button>
            ))}
          </div>
        </section>

        <section className="panel company-editor">
          <SectionTitle
            aside={
              <div className="company-editor-actions">
                {selected ? (
                  <>
                    <Badge tone="neutral">
                      已填 {stats.filled} / {stats.total}
                    </Badge>
                    <button
                      className="btn btn-ghost btn-sm danger-text"
                      disabled={saving || deleting}
                      onClick={() => {
                        void remove()
                      }}
                      type="button"
                    >
                      <Icon name="x" size={15} />
                      删除
                    </button>
                  </>
                ) : (
                  <Badge tone="info">新建资料</Badge>
                )}
              </div>
            }
            description={
              selected
                ? `正在编辑「${selected.name}」，保存后更新 PostgreSQL 中的同一条记录。`
                : '填写公司基本资料并保存；复杂业绩、人员、财务结构可在高级 JSON 中维护。'
            }
            title={selected ? '编辑公司资料' : '新建公司资料'}
          />

          {feedback ? <Notice tone="success">{feedback}</Notice> : null}
          {saveError ? <ErrorBanner message={saveError} /> : null}
          {jsonError ? <ErrorBanner message={jsonError} /> : null}

          <div className="completeness">
            <span style={{ width: `${stats.total ? Math.round((stats.filled / stats.total) * 100) : 0}%` }} />
          </div>

          <div className="form-section">
            <h3>公司基本信息</h3>
            <div className="form-grid">
              {COMPANY_FIELDS.map(([key, label]) => (
                <Field
                  key={key}
                  label={label}
                  onChange={(value) => applyProfile(withCompanyField(profile, key, value))}
                  placeholder={
                    key === 'name'
                      ? '请输入公司全称'
                      : key === 'established_date' || key === 'registered_capital'
                        ? '如：2010-05-01 / 5000 万元'
                        : undefined
                  }
                  type={key === 'email' ? 'email' : key === 'phone' ? 'tel' : 'text'}
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
                <Icon name="plus" size={15} />
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
                        onClick={() => applyProfile(removeQualification(profile, index))}
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
                            applyProfile(withQualificationField(profile, index, key, value))
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
                尚未添加资质。写标时缺失资质会保留【待填写：资质名称】等占位符。
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
                表单修改会实时写入 JSON；合法 JSON 也会同步回表单。公司资料仅保存在本机
                PostgreSQL，不会发送给外部服务。
              </p>
              <textarea
                aria-label="公司资料 JSON"
                className="json-editor"
                onChange={(event) => handleJsonChange(event.target.value)}
                spellCheck={false}
                value={jsonText}
              />
              {jsonError ? null : (
                <p className="json-synced">
                  <Icon name="check" size={15} />
                  当前 JSON 已同步到表单
                </p>
              )}
            </div>
          </details>

          <div className="company-save-bar">
            <span>
              保存范围：公司信息、资质、信用、安全及高级 JSON 中的全部结构。
            </span>
            <button
              className="btn btn-primary"
              disabled={saving || deleting || jsonError !== null}
              onClick={() => {
                void save()
              }}
              type="button"
            >
              {saving ? <Spinner /> : <Icon name="database" size={16} />}
              {selected ? '保存修改' : '保存到 PostgreSQL'}
            </button>
          </div>
        </section>
      </div>
    </div>
  )
}
