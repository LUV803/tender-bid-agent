import { useEffect, useRef, useState } from 'react'
import type { DocumentSummary, SearchHit, SearchMode, SearchResponse } from '../api'
import { errorMessage, getDocuments, searchTender } from '../api'
import { useApiData } from '../hooks'
import { Icon } from '../icons'
import {
  Badge,
  EmptyState,
  ErrorBanner,
  Notice,
  Spinner,
  ViewHeader,
  type Tone,
} from '../components/ui'

const EXAMPLES = [
  '投标报价得分怎么算',
  '施工组织设计怎么写',
  '资格审查需要什么材料',
]

const KIND_TONES: Record<string, Tone> = {
  table: 'info',
  factor: 'accent',
  section: 'neutral',
  qualification: 'success',
  review: 'warning',
  date: 'info',
  contract: 'neutral',
  technical: 'accent',
  format: 'neutral',
  other: 'neutral',
  fragment: 'info',
}

const KIND_LABELS: Record<string, string> = {
  table: '表格',
  section: '章节',
  factor: '评分因素',
  review: '评审标准',
  qualification: '资格要求',
  date: '关键时间',
  contract: '合同条款',
  technical: '技术要求',
  format: '投标文件格式',
  other: '其他要求',
  fragment: '原文片段',
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function Highlighted({ text, terms }: { text: string; terms: string[] }) {
  const unique = Array.from(new Set(terms.filter((term) => term.length >= 2)))
    .sort((left, right) => right.length - left.length)
    .slice(0, 8)
  if (!unique.length) return <>{text}</>
  const matcher = new RegExp(`(${unique.map(escapeRegExp).join('|')})`, 'gi')
  const lowered = new Set(unique.map((term) => term.toLowerCase()))
  return (
    <>
      {text.split(matcher).map((part, index) =>
        lowered.has(part.toLowerCase()) ? (
          <mark key={index}>{part}</mark>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </>
  )
}

function docLabel(doc: DocumentSummary): string {
  return doc.project_name || doc.source_name || doc.doc_id
}

function tableTitle(hit: SearchHit): string {
  return (
    hit.table_title ||
    hit.title.replace(/^表\s*\d+\s*[:：]\s*/, '') ||
    hit.title
  )
}

export function SearchView({
  initialDocId,
  focusToken,
  onGoUpload,
}: {
  initialDocId?: string | null
  focusToken?: number
  onGoUpload: () => void
}) {
  const documentsRequest = useApiData(getDocuments)
  const { data, loading, error, refresh } = documentsRequest
  const documents = data ?? []

  const [docId, setDocId] = useState(initialDocId ?? '')
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState<Exclude<SearchMode, 'hybrid'>>('auto')
  const [result, setResult] = useState<SearchResponse | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const requestId = useRef(0)

  const selected = documents.find((doc) => doc.doc_id === docId) ?? null
  const canSearch = Boolean(docId) && Boolean(query.trim()) && !searching

  useEffect(() => {
    if (!data?.length) return
    setDocId((current) => {
      if (current && data.some((doc) => doc.doc_id === current)) return current
      return (data.find((doc) => doc.has_extraction) ?? data[0]).doc_id
    })
  }, [data])

  useEffect(() => {
    if (initialDocId) setDocId(initialDocId)
  }, [initialDocId, focusToken])

  useEffect(() => {
    requestId.current += 1
    setResult(null)
    setSearchError(null)
    setSearching(false)
  }, [docId])

  function chooseDoc(next: string) {
    setDocId(next)
  }

  async function runSearch(nextQuery = query) {
    const trimmed = nextQuery.trim()
    if (!docId || !trimmed) return
    const current = ++requestId.current
    setQuery(trimmed)
    setSearching(true)
    setSearchError(null)
    setResult(null)
    try {
      const payload = await searchTender(docId, trimmed, { limit: 15, mode })
      if (requestId.current !== current) return
      setResult(payload)
    } catch (reason) {
      if (requestId.current !== current) return
      setSearchError(errorMessage(reason))
    } finally {
      if (requestId.current === current) setSearching(false)
    }
  }

  return (
    <div>
      <ViewHeader
        actions={
          <div className="header-badges">
            <Badge tone={mode === 'lexical' ? 'neutral' : 'info'} dot>
              {mode === 'lexical' ? '关键词模式' : '智能混合'}
            </Badge>
            <button
              className="btn btn-secondary btn-sm"
              disabled={loading}
              onClick={refresh}
              type="button"
            >
              <Icon name="refresh" size={16} />
              刷新文档
            </button>
          </div>
        }
        description="面向问题的招标依据检索：章节关键词、结构化评分/资格字段与向量语义共同排序，结果带页码与表号引用。"
        eyebrow="Step 02 · Search"
        title="招标文件检索"
      />

      {error ? <ErrorBanner message={error} onRetry={refresh} /> : null}

      {loading && !data ? (
        <section className="panel">
          <Spinner label="正在读取已解析文档" />
        </section>
      ) : null}

      {data && documents.length === 0 ? (
        <section className="panel">
          <EmptyState
            action={
              <button className="btn btn-primary" onClick={onGoUpload} type="button">
                上传第一份文档
                <Icon name="arrow-right" size={17} />
              </button>
            }
            description="检索基于解析产物工作。上传 PDF 并完成解析后，即可按问题查找评分办法、资格审查表和施工要求。"
            icon="search"
            title="还没有可检索的文档"
          />
        </section>
      ) : null}

      {documents.length > 0 ? (
        <>
          <section className="search-hero">
            <div className="search-hero-head">
              <div>
                <span className="eyebrow">Hybrid retrieval</span>
                <h2>这次想知道什么？</h2>
                <p>
                  直接输入自然语言问题，或带表号的精确查询。每条结果都会标注来自哪一页、
                  哪一节或哪张表。
                </p>
              </div>
              <span className="search-hero-mark">
                <Icon name="search" size={22} />
              </span>
            </div>

            <form
              className="search-form"
              onSubmit={(event) => {
                event.preventDefault()
                void runSearch()
              }}
            >
              <div className="search-fields">
                <label className="search-field">
                  <span>检索文档</span>
                  <select
                    onChange={(event) => chooseDoc(event.target.value)}
                    value={docId}
                  >
                    {documents.map((doc) => (
                      <option key={doc.doc_id} value={doc.doc_id}>
                        {docLabel(doc)}
                      </option>
                    ))}
                  </select>
                  {selected ? (
                    <small>
                      {selected.pages} 页 ·{' '}
                      {selected.has_extraction ? '已抽取' : '未抽取'} ·{' '}
                      {selected.has_index ? '已入库' : '未入库'}
                    </small>
                  ) : null}
                </label>

                <label className="search-field search-field-mode">
                  <span>检索方式</span>
                  <select
                    onChange={(event) =>
                      setMode(event.target.value as Exclude<SearchMode, 'hybrid'>)
                    }
                    value={mode}
                  >
                    <option value="auto">智能混合：关键词 + 结构化 + 向量</option>
                    <option value="lexical">关键词 + 结构化：不调用嵌入</option>
                  </select>
                  <small>
                    {mode === 'lexical'
                      ? '离线可用，响应更快'
                      : '向量不可用时自动降级'}
                  </small>
                </label>
              </div>

              <div className="search-query-row">
                <label className="search-query-box">
                  <Icon name="search" size={19} />
                  <input
                    aria-label="招标文件检索问题"
                    maxLength={200}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="例如：投标报价得分怎么算？"
                    value={query}
                  />
                  {query ? (
                    <button
                      aria-label="清空检索词"
                      className="search-clear"
                      onClick={() => setQuery('')}
                      type="button"
                    >
                      <Icon name="x" size={15} />
                    </button>
                  ) : null}
                </label>
                <button
                  className="btn btn-primary search-submit"
                  disabled={!canSearch}
                  type="submit"
                >
                  {searching ? <Spinner /> : <Icon name="search" size={17} />}
                  {searching ? '检索中' : '检索'}
                </button>
              </div>

              <div className="search-examples">
                <span>试试示例</span>
                {EXAMPLES.map((example) => (
                  <button
                    className="search-example"
                    disabled={searching}
                    key={example}
                    onClick={() => {
                      void runSearch(example)
                    }}
                    type="button"
                  >
                    {example}
                    <Icon name="arrow-right" size={13} />
                  </button>
                ))}
              </div>
            </form>
          </section>

          {searchError ? (
            <ErrorBanner
              message={searchError}
              onRetry={() => {
                void runSearch()
              }}
            />
          ) : null}

          {searching ? (
            <section className="panel search-loading">
              <Spinner label="正在合并关键词、结构化字段与向量结果" />
            </section>
          ) : null}

          {!searching && !result && !searchError ? (
            <section className="panel">
              <EmptyState
                description="输入问题后开始检索。示例：评分办法、资格审查材料、施工组织设计、投标保证金、关键时间。"
                icon="search"
                title="等待检索"
              />
            </section>
          ) : null}

          {result ? (
            <div className="search-output">
              <section className="search-summary">
                <div className="search-summary-copy">
                  <strong>{result.total} 条结果</strong>
                  <span>
                    {result.vector_used ? '混合检索' : '关键词 + 结构化'} ·{' '}
                    {result.elapsed_ms} ms · 「{result.query}」
                  </span>
                </div>
                <div className="search-summary-chips">
                  {Object.entries(result.counts).map(([kind, count]) => (
                    <span className="search-count-chip" key={kind}>
                      {KIND_LABELS[kind] ?? kind}
                      <strong>{count}</strong>
                    </span>
                  ))}
                </div>
              </section>

              {result.warnings.length ? (
                <Notice tone="warning" title="检索已降级">
                  {result.warnings.join('；')}
                </Notice>
              ) : null}

              {result.tables.length ? (
                <section className="search-related">
                  <div className="search-related-head">
                    <div>
                      <h2>相关表格</h2>
                      <p>按表号定位；点击可在当前文档中继续检索该表。</p>
                    </div>
                    <Badge tone="info">{result.tables.length} 张</Badge>
                  </div>
                  <div className="search-related-grid">
                    {result.tables.map((table, index) => (
                      <button
                        className="search-table-card"
                        key={`${table.table_label}-${index}`}
                        onClick={() => {
                          void runSearch(table.title)
                        }}
                        title="用该表标题继续检索"
                        type="button"
                      >
                        <span className="search-table-card-label">
                          {table.table_label ?? '表格'}
                        </span>
                        <strong>{tableTitle(table)}</strong>
                        <span className="search-table-card-meta">
                          {table.page_hint || '页码待定位'}
                          <span>相关度 {table.score.toFixed(2)}</span>
                        </span>
                      </button>
                    ))}
                  </div>
                </section>
              ) : null}

              {result.results.length === 0 ? (
                <section className="panel">
                  <EmptyState
                    action={
                      <button
                        className="btn btn-secondary btn-sm"
                        onClick={() => {
                          setQuery('')
                        }}
                        type="button"
                      >
                        清空重新输入
                      </button>
                    }
                    description="尝试缩短问题、换用招标文件中的原词，或直接输入表号（如「表10 投标报价」）。"
                    icon="search"
                    title="没有找到相关内容"
                  />
                </section>
              ) : (
                <section className="search-results">
                  {result.results.map((hit, index) => (
                    <article
                      className="search-result"
                      key={`${hit.kind}-${index}-${hit.title}`}
                    >
                      <span className="search-result-index">
                        {String(index + 1).padStart(2, '0')}
                      </span>
                      <div className="search-result-body">
                        <div className="search-result-top">
                          <Badge tone={KIND_TONES[hit.kind] ?? 'neutral'}>
                            {hit.kind_label || KIND_LABELS[hit.kind] || hit.kind}
                          </Badge>
                          <h3>{hit.title}</h3>
                          <span
                            className={`search-cite ${hit.page_hint ? '' : 'is-empty'}`}
                          >
                            {hit.page_hint || '页码待定位'}
                          </span>
                        </div>
                        <p className="search-snippet">
                          <Highlighted
                            terms={hit.matched_terms}
                            text={hit.snippet}
                          />
                        </p>
                        <div className="search-result-foot">
                          {hit.section_path ? (
                            <span className="search-path">{hit.section_path}</span>
                          ) : null}
                          <span className="search-source">
                            {hit.source === 'vector' ? '向量召回' : '关键词命中'}
                          </span>
                          {hit.matched_terms.length ? (
                            <span className="search-terms">
                              命中：{hit.matched_terms.slice(0, 6).join(' · ')}
                            </span>
                          ) : null}
                          <span className="search-score">
                            相关度 {hit.score.toFixed(2)}
                          </span>
                        </div>
                      </div>
                    </article>
                  ))}
                </section>
              )}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  )
}
