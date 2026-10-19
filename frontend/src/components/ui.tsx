import type { ReactNode } from 'react'
import { Icon, type IconName } from '../icons'

export type Tone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger' | 'info'

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="spinner-wrap">
      <span className="spinner" />
      {label ? <span>{label}</span> : null}
    </span>
  )
}

export function Badge({
  children,
  tone = 'neutral',
  dot = false,
}: {
  children: ReactNode
  tone?: Tone
  dot?: boolean
}) {
  return (
    <span className={`badge badge-${tone}`}>
      {dot ? <span className="badge-dot" /> : null}
      {children}
    </span>
  )
}

export function ErrorBanner({
  message,
  onRetry,
}: {
  message: string
  onRetry?: () => void
}) {
  return (
    <div className="error-banner" role="alert">
      <Icon name="alert" size={18} />
      <span>{message}</span>
      {onRetry ? (
        <button className="btn btn-sm btn-ghost" onClick={onRetry} type="button">
          重试
        </button>
      ) : null}
    </div>
  )
}

export function Notice({
  tone = 'info',
  title,
  children,
}: {
  tone?: 'info' | 'warning' | 'success'
  title?: string
  children: ReactNode
}) {
  const icon: IconName =
    tone === 'warning' ? 'alert' : tone === 'success' ? 'check' : 'sparkles'
  return (
    <div className={`notice notice-${tone}`}>
      <Icon name={icon} size={18} />
      <div>
        {title ? <strong>{title}</strong> : null}
        <div className="notice-body">{children}</div>
      </div>
    </div>
  )
}

export function EmptyState({
  icon = 'file',
  title,
  description,
  action,
}: {
  icon?: IconName
  title: string
  description: string
  action?: ReactNode
}) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <Icon name={icon} size={28} />
      </span>
      <h3>{title}</h3>
      <p>{description}</p>
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  )
}

export function ViewHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string
  title: string
  description: string
  actions?: ReactNode
}) {
  return (
    <header className="view-header">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions ? <div className="view-actions">{actions}</div> : null}
    </header>
  )
}

export function Metric({
  label,
  value,
  hint,
  icon,
  tone = 'neutral',
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
  icon?: IconName
  tone?: 'neutral' | 'accent' | 'warning' | 'danger' | 'success'
}) {
  return (
    <div className={`metric metric-${tone}`}>
      <div className="metric-top">
        <span>{label}</span>
        {icon ? <Icon name={icon} size={17} /> : null}
      </div>
      <div className="metric-value">{value}</div>
      {hint ? <div className="metric-hint">{hint}</div> : null}
    </div>
  )
}

export interface ProcessStep {
  key: string
  label: string
  detail?: string
  state: 'pending' | 'active' | 'done' | 'error'
}

export function ProcessSteps({ steps }: { steps: ProcessStep[] }) {
  return (
    <ol className="process-steps">
      {steps.map((step, index) => (
        <li className={`process-step step-${step.state}`} key={step.key}>
          <span className="step-marker">
            {step.state === 'done' ? (
              <Icon name="check" size={14} />
            ) : step.state === 'error' ? (
              <Icon name="x" size={14} />
            ) : step.state === 'active' ? (
              <span className="step-pulse" />
            ) : (
              index + 1
            )}
          </span>
          <span className="step-copy">
            <strong>{step.label}</strong>
            {step.detail ? <small>{step.detail}</small> : null}
          </span>
        </li>
      ))}
    </ol>
  )
}

export function ProgressLog({ messages }: { messages: string[] }) {
  if (!messages.length) return null
  return (
    <div className="progress-log" aria-live="polite">
      {messages.slice(-40).map((message, index) => (
        <div className="progress-line" key={`${index}-${message}`}>
          <span>{String(index + 1).padStart(2, '0')}</span>
          <p>{message}</p>
        </div>
      ))}
    </div>
  )
}

export function SearchInput({
  value,
  onChange,
  placeholder,
}: {
  value: string
  onChange: (value: string) => void
  placeholder: string
}) {
  return (
    <label className="search-input">
      <Icon name="search" size={17} />
      <input
        aria-label={placeholder}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        type="search"
        value={value}
      />
    </label>
  )
}

export function SectionTitle({
  title,
  description,
  aside,
}: {
  title: string
  description?: string
  aside?: ReactNode
}) {
  return (
    <div className="section-title">
      <div>
        <h2>{title}</h2>
        {description ? <p>{description}</p> : null}
      </div>
      {aside ? <div className="section-actions">{aside}</div> : null}
    </div>
  )
}
