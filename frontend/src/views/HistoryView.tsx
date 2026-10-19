import { useCallback, useState } from 'react'
import { getBidDetail, getBidHistory } from '../api'
import { formatDate, formatScore, formatDuration } from '../format'
import { useApiData } from '../hooks'
import { Icon } from '../icons'
import { BidReport } from '../components/BidReport'
import {
  Badge,
  EmptyState,
  ErrorBanner,
  SearchInput,
  Spinner,
  ViewHeader,
} from '../components/ui'

function HistoryDetail({ docId }: { docId: string }) {
  const load = useCallback(() => getBidDetail(docId), [docId])
  const { data, loading, error, refresh } = useApiData(load)

  if (!data && loading) {
    return (
      <section className="panel">
        <Spinner label="正在读取产物详情" />
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
  return <BidReport detail={data} />
}

export function HistoryView({
  focusDocId,
  onFocusConsumed,
  onGoBid,
}: {
  focusDocId: string | null
  onFocusConsumed: () => void
  onGoBid: () => void
}) {
  const { data, loading, error, refresh } = useApiData(getBidHistory)
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const history = data ?? []
  const needle = query.trim().toLowerCase()
  const filtered = needle
    ? history.filter((item) =>
        [item.project_name, item.bidder, item.doc_id]
          .join(' ')
          .toLowerCase()
          .includes(needle),
      )
    : history

  const activeDocId =
    focusDocId ??
    (selectedDocId && history.some((item) => item.doc_id === selectedDocId)
      ? selectedDocId
      : (history[0]?.doc_id ?? null))
  const selected = history.find((item) => item.doc_id === activeDocId) ?? null

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
        description="浏览 upload/bids 下由 CLI、网页或桌面端生成的投标产物，支持在线预览、下载与打开文件夹。"
        eyebrow="Step 06 · History"
        title="历史投标产物"
      />

      {error ? <ErrorBanner message={error} onRetry={refresh} /> : null}
      {loading && !data ? (
        <section className="panel">
          <Spinner label="正在扫描历史产物" />
        </section>
      ) : null}

      {data && history.length === 0 ? (
        <section className="panel">
          <EmptyState
            action={
              <button className="btn btn-primary" onClick={onGoBid} type="button">
                去编写投标文件
                <Icon name="arrow-right" size={17} />
              </button>
            }
            description="完成一次投标文件编写后，bid.md、bid.docx 与内部审查报告会出现在这里。"
            icon="history"
            title="还没有历史投标产物"
          />
        </section>
      ) : null}

      {history.length ? (
        <div className="history-layout">
          <aside className="panel history-sidebar">
            <div className="history-sidebar-head">
              <div>
                <span className="eyebrow">Archive</span>
                <h2>{history.length} 份产物</h2>
              </div>
            </div>
            <SearchInput
              onChange={setQuery}
              placeholder="搜索项目、投标人或 doc_id"
              value={query}
            />
            {filtered.length ? (
              <div className="history-list">
                {filtered.map((item) => {
                  const stats = item.stats
                  const active = item.doc_id === activeDocId
                  return (
                    <button
                      className={`history-item ${active ? 'is-active' : ''}`}
                      key={item.doc_id}
                      onClick={() => {
                        setSelectedDocId(item.doc_id)
                        onFocusConsumed()
                      }}
                      type="button"
                    >
                      <div className="history-item-head">
                        <strong>{item.project_name || item.doc_id}</strong>
                        <span>{formatDate(item.created_at)}</span>
                      </div>
                      <p>{item.bidder || '投标人资料未填写'}</p>
                      <div className="history-item-meta">
                        {stats.high_issues ? (
                          <Badge tone="danger">高风险 {formatScore(stats.high_issues)}</Badge>
                        ) : null}
                        {stats.issues ? (
                          <Badge tone="warning">
                            问题 {formatScore(stats.issues)}
                          </Badge>
                        ) : null}
                        {stats.pending_markers ? (
                          <Badge tone="neutral">
                            待填 {formatScore(stats.pending_markers)}
                          </Badge>
                        ) : null}
                        <span className="history-duration">
                          {formatDuration(item.elapsed_seconds)}
                        </span>
                      </div>
                    </button>
                  )
                })}
              </div>
            ) : (
              <EmptyState
                description="试试其他项目名称或投标人关键词。"
                icon="search"
                title="没有匹配的产物"
              />
            )}
          </aside>

          <div className="history-detail">
            {selected ? (
              <HistoryDetail docId={selected.doc_id} key={selected.doc_id} />
            ) : (
              <section className="panel">
                <EmptyState
                  description="从左侧列表选择一份投标产物查看评分覆盖与审查报告。"
                  icon="history"
                  title="选择一份产物"
                />
              </section>
            )}
          </div>
        </div>
      ) : null}
    </div>
  )
}
