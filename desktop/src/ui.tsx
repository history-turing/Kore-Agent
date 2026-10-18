import type { ReactNode } from 'react'
import { RefreshCw, Zap } from 'lucide-react'

export function Page({
  title,
  subtitle,
  actions,
  children,
}: {
  title: string
  subtitle: string
  actions?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1>{title}</h1>
          <p>{subtitle}</p>
        </div>
        {actions}
      </header>
      <div className="page-body">{children}</div>
    </section>
  )
}
export function Empty({
  icon: Icon = Zap,
  title,
  hint,
  action,
}: {
  icon?: typeof Zap
  title: string
  hint: string
  action?: ReactNode
}) {
  return (
    <div className="empty">
      <Icon size={28} />
      <h3>{title}</h3>
      <p>{hint}</p>
      {action}
    </div>
  )
}
export function Busy() {
  return (
    <div className="busy" role="status">
      <RefreshCw size={18} />
      <span>加载中</span>
    </div>
  )
}
export function ErrorBox({ error }: { error: unknown }) {
  return (
    <div className="error-box" role="alert">
      {error instanceof Error ? error.message : String(error)}
    </div>
  )
}
export function statusChinese(value: string) {
  return (
    (
      {
        pending: '待处理',
        active: '进行中',
        running: '运行中',
        blocked: '受阻',
        completed: '已完成',
        failed: '失败',
        interrupted: '已中断',
        cancelled: '已取消',
        paused: '已暂停',
        approved: '已允许',
        denied: '已拒绝',
        available: '可用',
        enabled: '已启用',
        disabled: '已停用',
        ready: '待审核',
        observing: '观察中',
        unavailable: '不可用',
        restart_required: '需重启',
        archived: '已归档',
      } as Record<string, string>
    )[value] || value
  )
}
export function Status({ value }: { value: string }) {
  return <span className={`status ${value}`}>{statusChinese(value)}</span>
}

export function openUrl(url: string) {
  if (window.koreNext?.openExternal) window.koreNext.openExternal(url)
  else window.open(url, '_blank', 'noopener,noreferrer')
}

export function formatBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1048576) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1048576).toFixed(1)} MB`
}
