import { useEffect, useState } from 'react'
import type { BidArtifact } from '../api'
import { apiUrl, errorMessage, getArtifactText } from '../api'
import { formatBytes } from '../format'
import { Icon } from '../icons'
import { MarkdownView } from './MarkdownView'
import { Modal } from './Modal'
import { ErrorBanner, Spinner } from './ui'

function PreviewModal({
  artifact,
  onClose,
}: {
  artifact: BidArtifact | null
  onClose: () => void
}) {
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!artifact) return
    let cancelled = false
    setLoading(true)
    setText('')
    setError(null)
    getArtifactText(artifact.content_url)
      .then((value) => {
        if (!cancelled) setText(value)
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(errorMessage(reason))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [artifact])

  return (
    <Modal
      footer={
        artifact ? (
          <a
            className="btn btn-secondary btn-sm"
            href={apiUrl(artifact.download_url)}
            rel="noreferrer"
          >
            <Icon name="download" size={16} />
            下载此文件
          </a>
        ) : undefined
      }
      onClose={onClose}
      open={artifact !== null}
      subtitle={artifact ? `${artifact.name} · ${formatBytes(artifact.size)}` : ''}
      title={artifact?.label ?? '文件预览'}
      wide
    >
      {loading ? <Spinner label="正在读取文件" /> : null}
      {error ? <ErrorBanner message={error} /> : null}
      {!loading && !error ? <MarkdownView text={text} /> : null}
    </Modal>
  )
}

export function ArtifactActions({
  artifacts,
  folder,
  compact = false,
}: {
  artifacts: BidArtifact[]
  folder: string
  compact?: boolean
}) {
  const [preview, setPreview] = useState<BidArtifact | null>(null)
  const [folderMessage, setFolderMessage] = useState<string | null>(null)
  const markdown = artifacts.find((item) => item.key === 'markdown')
  const docx = artifacts.find((item) => item.key === 'docx')
  const review = artifacts.find((item) => item.key === 'review')
  const manifest = artifacts.find((item) => item.key === 'manifest')

  async function openFolder() {
    setFolderMessage(null)
    const bridge = window.bidding
    if (!bridge?.openBidFolder) {
      setFolderMessage('网页版无法打开本地文件夹，请使用 Electron 桌面版。')
      return
    }
    try {
      const result = await bridge.openBidFolder(folder)
      if (!result.ok) {
        setFolderMessage(result.error || '无法打开文件夹')
      }
    } catch (reason) {
      setFolderMessage(errorMessage(reason))
    }
  }

  return (
    <div className={`artifact-actions ${compact ? 'artifact-actions-compact' : ''}`}>
      <div className="artifact-buttons">
        {markdown ? (
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => setPreview(markdown)}
            type="button"
          >
            <Icon name="eye" size={16} />
            预览标书
          </button>
        ) : null}
        {docx ? (
          <a
            className="btn btn-primary btn-sm"
            href={apiUrl(docx.download_url)}
            rel="noreferrer"
          >
            <Icon name="download" size={16} />
            下载 Word
          </a>
        ) : null}
        {markdown ? (
          <a
            className="btn btn-ghost btn-sm"
            href={apiUrl(markdown.download_url)}
            rel="noreferrer"
          >
            下载 Markdown
          </a>
        ) : null}
        {review ? (
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => setPreview(review)}
            type="button"
          >
            审查报告
          </button>
        ) : null}
        <button
          className="btn btn-ghost btn-sm"
          onClick={() => {
            void openFolder()
          }}
          type="button"
        >
          <Icon name="folder" size={16} />
          打开文件夹
        </button>
        {manifest ? (
          <a
            className="artifact-link"
            href={apiUrl(manifest.download_url)}
            rel="noreferrer"
          >
            下载清单
          </a>
        ) : null}
      </div>
      {folderMessage ? <p className="artifact-hint">{folderMessage}</p> : null}
      <PreviewModal artifact={preview} onClose={() => setPreview(null)} />
    </div>
  )
}
