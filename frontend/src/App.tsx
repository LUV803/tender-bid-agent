import { useCallback, useState } from 'react'
import './App.css'
import { apiUrl } from './api'
import { useHealth } from './hooks'
import { Icon, type IconName } from './icons'
import { useTheme } from './theme'
import { BidView } from './views/BidView'
import { CompanyView } from './views/CompanyView'
import { DocumentsView } from './views/DocumentsView'
import { HistoryView } from './views/HistoryView'
import { SearchView } from './views/SearchView'
import { UploadView } from './views/UploadView'

type ViewKey = 'upload' | 'search' | 'bid' | 'companies' | 'documents' | 'history'

const NAV: Array<{
  key: ViewKey
  step: string
  title: string
  subtitle: string
  icon: IconName
}> = [
  {
    key: 'upload',
    step: '01',
    title: '上传解析',
    subtitle: 'PDF → 结构化全文',
    icon: 'upload',
  },
  {
    key: 'search',
    step: '02',
    title: '招标检索',
    subtitle: '页码与表号定位',
    icon: 'search',
  },
  {
    key: 'bid',
    step: '03',
    title: '投标编写',
    subtitle: '多 Agent 生成标书',
    icon: 'pen',
  },
  {
    key: 'companies',
    step: '04',
    title: '公司资料',
    subtitle: 'PostgreSQL 持久化',
    icon: 'database',
  },
  {
    key: 'documents',
    step: '05',
    title: '文档库',
    subtitle: '已解析文档',
    icon: 'library',
  },
  {
    key: 'history',
    step: '06',
    title: '历史产物',
    subtitle: '预览与下载',
    icon: 'history',
  },
]

function App() {
  const [view, setView] = useState<ViewKey>('upload')
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null)
  const [historyFocus, setHistoryFocus] = useState<string | null>(null)
  const [searchFocus, setSearchFocus] = useState<{
    docId: string
    token: number
  } | null>(null)
  const { health, online } = useHealth()
  const { theme, toggleTheme } = useTheme()
  const active = NAV.find((item) => item.key === view) ?? NAV[0]
  const themeTarget = theme === 'dark' ? '浅色' : '深色'

  const goWrite = useCallback((docId: string) => {
    setSelectedDocId(docId)
    setView('bid')
  }, [])

  const goUpload = useCallback(() => setView('upload'), [])
  const onSelectDoc = useCallback((docId: string) => setSelectedDocId(docId), [])

  const openHistory = useCallback((docId: string) => {
    setHistoryFocus(docId)
    setView('history')
  }, [])

  const consumeHistoryFocus = useCallback(() => setHistoryFocus(null), [])

  const openSearch = useCallback((docId: string) => {
    setSearchFocus((current) => ({
      docId,
      token: (current?.token ?? 0) + 1,
    }))
    setView('search')
  }, [])

  const serviceTone =
    online === null ? 'pending' : online ? 'online' : 'offline'
  const serviceText =
    online === null ? '检测中' : online ? '服务正常' : '服务离线'

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <Icon name="document-check" size={22} />
          </span>
          <div className="brand-copy">
            <strong>标书工坊</strong>
            <span>BIDDING AGENT</span>
          </div>
        </div>

        <nav className="step-nav">
          <div className="nav-label">工作流</div>
          {NAV.map((item) => (
            <button
              className={`nav-item ${view === item.key ? 'is-active' : ''}`}
              key={item.key}
              onClick={() => setView(item.key)}
              type="button"
            >
              <span className="nav-step">{item.step}</span>
              <span className="nav-icon">
                <Icon name={item.icon} size={18} />
              </span>
              <span className="nav-copy">
                <strong>{item.title}</strong>
                <small>{item.subtitle}</small>
              </span>
              {view === item.key ? (
                <span className="nav-active-mark">
                  <Icon name="arrow-right" size={14} />
                </span>
              ) : null}
            </button>
          ))}
        </nav>

        <div className="sidebar-note">
          <div className="sidebar-note-head">
            <Icon name="shield" size={16} />
            <strong>事实可追溯</strong>
          </div>
          <p>
            招标事实只来自解析与抽取产物，投标人事实只来自本项目资料；缺失字段保留
            【待填写】占位符。
          </p>
          <div className="sidebar-tags">
            <span>固定 11 章</span>
            <span>内部审查</span>
          </div>
        </div>

        <div className="sidebar-foot">
          <span className={`status-dot ${serviceTone}`} />
          <div>
            <strong>{online === false ? '后端未连接' : '本机工作区'}</strong>
            <small>
              {health?.backend ? `${health.backend.toUpperCase()} · ` : ''}
              FastAPI 8001
            </small>
          </div>
        </div>
      </aside>

      <div className="main-column">
        <header className="topbar">
          <div className="topbar-title">
            <span className="topbar-kicker">Tender Intelligence</span>
            <div>
              <h1>招标文件多智能体系统</h1>
              <span className="topbar-context">/ {active.title}</span>
            </div>
          </div>
          <div className="topbar-actions">
            <button
              aria-label={`切换到${themeTarget}主题`}
              className="theme-toggle"
              onClick={toggleTheme}
              title={`切换到${themeTarget}主题`}
              type="button"
            >
              <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={16} />
              <span>{themeTarget}</span>
            </button>
            <span className={`service-pill ${serviceTone}`}>
              <span className={`status-dot ${serviceTone}`} />
              {serviceText}
              <strong>8001</strong>
            </span>
            <a
              className="btn btn-ghost btn-sm topbar-link"
              href={apiUrl('/docs')}
              rel="noreferrer"
              target="_blank"
            >
              API 文档
              <Icon name="external" size={14} />
            </a>
          </div>
        </header>

        <main className="workspace">
          <div className="view-pane" hidden={view !== 'upload'}>
            <UploadView onWrite={goWrite} />
          </div>
          <div className="view-pane" hidden={view !== 'search'}>
            <SearchView
              focusToken={searchFocus?.token}
              initialDocId={searchFocus?.docId}
              onGoUpload={goUpload}
            />
          </div>
          <div className="view-pane" hidden={view !== 'bid'}>
            <BidView
              onGoUpload={goUpload}
              onSelectDoc={onSelectDoc}
              selectedDocId={selectedDocId}
            />
          </div>
          <div className="view-pane" hidden={view !== 'companies'}>
            <CompanyView />
          </div>
          <div className="view-pane" hidden={view !== 'documents'}>
            <DocumentsView
              onGoUpload={goUpload}
              onOpenHistory={openHistory}
              onSearch={openSearch}
              onWrite={goWrite}
            />
          </div>
          <div className="view-pane" hidden={view !== 'history'}>
            <HistoryView
              focusDocId={historyFocus}
              onFocusConsumed={consumeHistoryFocus}
              onGoBid={() => setView('bid')}
            />
          </div>
        </main>
      </div>
    </div>
  )
}

export default App
