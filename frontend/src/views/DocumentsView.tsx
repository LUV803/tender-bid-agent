import { useState } from 'react'
import { getDocuments } from '../api'
import { formatDate, formatNumber, shortDocId } from '../format'
import { useApiData } from '../hooks'
import { Icon } from '../icons'
import {
  Badge,
  EmptyState,
  ErrorBanner,
  SearchInput,
  Spinner,
  ViewHeader,
} from '../components/ui'

export function DocumentsView({
  onWrite,
  onOpenHistory,
  onGoUpload,
}: {
  onWrite: (docId: string) => void
  onOpenHistory: (docId: string) => void
  onGoUpload: () => void
}) {
  const { data, loading, error, refresh } = useApiData(getDocuments)
  const [query, setQuery] = useState('')
  const documents = data ?? []
  const needle = query.trim().toLowerCase()
  const filtered = needle
    ? documents.filter((doc) =>
        [doc.project_name, doc.source_name, doc.doc_id, doc.doc_type]
          .join(' ')
          .toLowerCase()
          .includes(needle),
      )
    : documents

  const extracted = documents.filter((doc) => doc.has_extraction).length
  const indexed = documents.filter((doc) => doc.has_index).length
  const withBids = documents.filter((doc) => doc.bid).length

  return (
    <div>
      <ViewHeader
        actions={
          <button
            className="btn btn-secondary btn-sm"
            disabled={loading}
            onClick={refresh}
            type="button"
          >
            <Icon name="refresh" size={16} />
            刷新
          </button>
        }
        description="扫描 upload/parsed 下的解析产物，可直接选择已完成抽取的文档发起编写。"
        eyebrow="Step 03 · Library"
        title="已解析文档库"
      />

      <section className="library-stats">
        <div>
          <strong>{formatNumber(documents.length)}</strong>
          <span>已解析文档</span>
        </div>
        <div>
          <strong>{formatNumber(extracted)}</strong>
          <span>已完成抽取</span>
        </div>
        <div>
          <strong>{formatNumber(indexed)}</strong>
          <span>已写入向量库</span>
        </div>
        <div>
          <strong>{formatNumber(withBids)}</strong>
          <span>已有投标产物</span>
        </div>
      </section>

      <div className="list-toolbar">
        <SearchInput
          onChange={setQuery}
          placeholder="搜索项目名称、文件名或 doc_id"
          value={query}
        />
        <span className="toolbar-count">
          {needle ? `${filtered.length} 个匹配` : '按解析时间倒序'}
        </span>
      </div>

      {error ? <ErrorBanner message={error} onRetry={refresh} /> : null}
      {loading && !data ? (
        <section className="panel">
          <Spinner label="正在扫描文档库" />
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
            description="上传 PDF 并完成解析后，文档会自动出现在这里；使用 CLI 解析的文档也会被扫描到。"
            icon="library"
            title="还没有已解析文档"
          />
        </section>
      ) : null}

      {data && documents.length > 0 && filtered.length === 0 ? (
        <section className="panel">
          <EmptyState
            description="尝试搜索项目名称、源文件名或 doc_id。"
            icon="search"
            title="没有匹配的文档"
          />
        </section>
      ) : null}

      {filtered.length ? (
        <div className="document-grid">
          {filtered.map((doc) => (
            <article className="doc-card" key={doc.doc_id}>
              <div className="doc-card-top">
                <span className="doc-card-icon">
                  <Icon name="file" size={20} />
                </span>
                <div className="doc-card-title">
                  <h2>{doc.project_name || doc.source_name || doc.doc_id}</h2>
                  <p>
                    <code className="inline-code">{shortDocId(doc.doc_id, 30)}</code>
                    {doc.doc_type ? <span>{doc.doc_type}</span> : null}
                  </p>
                </div>
                {doc.bid ? <Badge tone="accent">已生成标书</Badge> : null}
              </div>

              <div className="doc-card-badges">
                <Badge tone={doc.has_extraction ? 'success' : 'warning'}>
                  {doc.has_extraction ? '已抽取' : '未抽取'}
                </Badge>
                {doc.extraction_partial ? (
                  <Badge tone="warning">抽取部分完成</Badge>
                ) : null}
                <Badge tone={doc.has_index ? 'info' : 'neutral'}>
                  {doc.has_index ? '已入库' : '未入库'}
                </Badge>
                {doc.ungrounded_count > 0 ? (
                  <Badge tone="warning">
                    {formatNumber(doc.ungrounded_count)} 个待核对字段
                  </Badge>
                ) : null}
              </div>

              <dl className="doc-facts">
                <div>
                  <dt>页数</dt>
                  <dd>{formatNumber(doc.pages)}</dd>
                </div>
                <div>
                  <dt>章节</dt>
                  <dd>{formatNumber(doc.section_count)}</dd>
                </div>
                <div>
                  <dt>评分项</dt>
                  <dd>
                    {doc.has_extraction
                      ? formatNumber(doc.evaluation_factor_count)
                      : '—'}
                  </dd>
                </div>
                <div>
                  <dt>解析时间</dt>
                  <dd>{formatDate(doc.parsed_at)}</dd>
                </div>
              </dl>

              <div className="doc-card-foot">
                <span className="doc-source" title={doc.source_name}>
                  {doc.source_name || '未记录源文件名'}
                  {doc.warning_count > 0
                    ? ` · ${formatNumber(doc.warning_count)} 条警告`
                    : ''}
                </span>
                <div className="doc-actions">
                  {doc.bid ? (
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={() => onOpenHistory(doc.doc_id)}
                      type="button"
                    >
                      查看产物
                    </button>
                  ) : null}
                  <button
                    className="btn btn-primary btn-sm"
                    disabled={!doc.has_extraction}
                    onClick={() => onWrite(doc.doc_id)}
                    title={
                      doc.has_extraction
                        ? '基于该文档编写投标文件'
                        : '请先完成结构化抽取'
                    }
                    type="button"
                  >
                    <Icon name="pen" size={15} />
                    编写标书
                  </button>
                </div>
              </div>
            </article>
          ))}
        </div>
      ) : null}
    </div>
  )
}
