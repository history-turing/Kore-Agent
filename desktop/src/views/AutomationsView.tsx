import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Clock3, Plus } from 'lucide-react'
import { useConfirmAction } from '../ConfirmDialog'
import { rpc } from '../rpc'
import type { Automation } from '../types'
import { Empty, ErrorBox, Page, Status } from '../ui'

export function Automations() {
  const confirmAction = useConfirmAction()
  const query = useQuery({
    queryKey: ['automations'],
    queryFn: () => rpc.call<{ automations: Automation[] }>('automation.list', {}).then((x) => x.automations),
    refetchInterval: 30000,
  })
  const [open, setOpen] = useState(false)
  const action = useMutation({
    mutationFn: ({ method, id }: { method: string; id: string }) => rpc.call(method, { automation_id: id }),
    onSuccess: () => query.refetch(),
  })
  return (
    <Page
      title="自动化"
      subtitle="按明确的时区定时或周期运行 Agent 工作。"
      actions={
        <button className="primary" onClick={() => setOpen(true)}>
          <Plus size={15} />
          新建
        </button>
      }
    >
      {open && (
        <AutomationForm
          close={() => setOpen(false)}
          done={() => {
            setOpen(false)
            query.refetch()
          }}
        />
      )}
      <div className="card-grid">
        {query.data?.map((item) => (
          <article className="card" key={item.id}>
            <div className="card-head">
              <Clock3 />
              <Status value={item.status} />
            </div>
            <h3>{item.title}</h3>
            <p>{item.prompt}</p>
            <dl>
              <dt>计划</dt>
              <dd>{scheduleLabel(item)}</dd>
              <dt>时区</dt>
              <dd>{item.schedule.timezone}</dd>
              <dt>下次运行</dt>
              <dd>{item.next_run_at ? new Date(item.next_run_at).toLocaleString() : '—'}</dd>
              <dt>上次运行</dt>
              <dd>{item.last_run_at ? new Date(item.last_run_at).toLocaleString() : '尚未运行'}</dd>
            </dl>
            <div className="card-actions">
              {item.status === 'active' ? (
                <button onClick={() => action.mutate({ method: 'automation.pause', id: item.id })}>
                  暂停
                </button>
              ) : item.status === 'paused' ? (
                <button onClick={() => action.mutate({ method: 'automation.resume', id: item.id })}>
                  恢复
                </button>
              ) : null}
              <button
                className="danger"
                onClick={() => {
                  void confirmAction({
                    title: `取消“${item.title}”？`,
                    message: '这项自动化将停止后续触发，已经创建的运行不受影响。',
                    confirmLabel: '取消自动化',
                    destructive: true,
                  }).then((yes) => {
                    if (yes) action.mutate({ method: 'automation.cancel', id: item.id })
                  })
                }}
              >
                取消
              </button>
            </div>
          </article>
        ))}
      </div>
      {query.error && <ErrorBox error={query.error} />}{' '}
      {!query.isLoading && !query.data?.length && (
        <Empty icon={Clock3} title="暂无自动化" hint="创建一次性、间隔或 Cron 工作。" />
      )}
    </Page>
  )
}
function scheduleLabel(item: Automation) {
  if (item.schedule.kind === 'once')
    return `一次 · ${item.schedule.run_at ? new Date(item.schedule.run_at).toLocaleString() : '未设置'}`
  if (item.schedule.kind === 'interval') return `每 ${item.schedule.interval_seconds} 秒`
  return `Cron ${item.schedule.cron_expr}`
}
function AutomationForm({ close, done }: { close(): void; done(): void }) {
  const [form, setForm] = useState({
    title: '',
    prompt: '',
    kind: 'once',
    run_at: '',
    interval_seconds: '3600',
    cron_expr: '0 9 * * *',
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai',
  })
  const title = useRef<HTMLInputElement>(null)
  useEffect(() => {
    title.current?.focus()
    const esc = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close()
    }
    window.addEventListener('keydown', esc)
    return () => window.removeEventListener('keydown', esc)
  }, [close])
  const mutation = useMutation({
    mutationFn: () =>
      rpc.call('automation.create', {
        ...form,
        run_at: form.run_at ? new Date(form.run_at).toISOString() : undefined,
        interval_seconds: Number(form.interval_seconds) || undefined,
      }),
    onSuccess: done,
  })
  return (
    <div
      className="modal-back"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close()
      }}
    >
      <form
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="automation-title"
        onSubmit={(event) => {
          event.preventDefault()
          mutation.mutate()
        }}
      >
        <h2 id="automation-title">新建自动化</h2>
        <label>
          标题
          <input
            ref={title}
            value={form.title}
            onChange={(e) => setForm({ ...form, title: e.target.value })}
            required
          />
        </label>
        <label>
          任务
          <textarea
            value={form.prompt}
            onChange={(e) => setForm({ ...form, prompt: e.target.value })}
            required
          />
        </label>
        <label>
          类型
          <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
            <option value="once">一次</option>
            <option value="interval">间隔</option>
            <option value="cron">Cron</option>
          </select>
        </label>
        {form.kind === 'once' && (
          <label>
            运行时间
            <input
              type="datetime-local"
              value={form.run_at}
              onChange={(e) => setForm({ ...form, run_at: e.target.value })}
              required
            />
          </label>
        )}
        {form.kind === 'interval' && (
          <label>
            间隔秒数
            <input
              type="number"
              min="1"
              value={form.interval_seconds}
              onChange={(e) => setForm({ ...form, interval_seconds: e.target.value })}
              required
            />
          </label>
        )}
        {form.kind === 'cron' && (
          <label>
            Cron 表达式
            <input
              value={form.cron_expr}
              onChange={(e) => setForm({ ...form, cron_expr: e.target.value })}
              required
            />
          </label>
        )}
        <label>
          IANA 时区
          <input
            value={form.timezone}
            onChange={(e) => setForm({ ...form, timezone: e.target.value })}
            required
          />
          <small>例如 Asia/Shanghai</small>
        </label>
        <div className="modal-actions">
          <button type="button" onClick={close}>
            取消
          </button>
          <button className="primary" type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? '创建中' : '创建'}
          </button>
        </div>
        {mutation.error && <ErrorBox error={mutation.error} />}
      </form>
    </div>
  )
}
