import { useEffect, useMemo, useState } from 'react'
import { CheckCircle2, ShieldAlert, XCircle } from 'lucide-react'
import { ApprovalControls } from './ApprovalControls'
import { approvalFacts, approvalTitle } from './approvalPresentation'
import { rpc } from './rpc'
import type { Approval, PermissionMatch, PermissionScope } from './types'
import { queueDesktopApproval, resolveApproval } from './liveState'

export default function ApprovalWindow() {
  const [queue, setQueue] = useState<Approval[]>([])
  const [busy, setBusy] = useState(false)
  const [scope, setScope] = useState<PermissionScope>('once')
  const [match, setMatch] = useState<PermissionMatch>('arguments')
  const current = queue[0]

  useEffect(() => {
    rpc.connect()
    const load = () =>
      rpc
        .call<{ approvals: Approval[] }>('approval.list', { status: 'pending', limit: 50 })
        .then((result) =>
          setQueue(
            result.approvals.filter(
              (item) => item.ui_scope === 'desktop' || item.tool_name.startsWith('computer_'),
            ),
          ),
        )
        .catch(() => setQueue([]))
    void load()
    const required = rpc.on('approval.required', (approval: Approval) =>
      setQueue((items) => queueDesktopApproval(items, approval)),
    )
    const resolved = rpc.on('approval.resolved', (approval: Approval) =>
      setQueue((items) => resolveApproval(items, approval.id)),
    )
    return () => {
      required()
      resolved()
    }
  }, [])

  const summary = useMemo(() => (current ? JSON.stringify(current.arguments, null, 2) : ''), [current])
  const decide = async (yes: boolean) => {
    if (!current || busy) return
    setBusy(true)
    try {
      await rpc.call(yes ? 'approval.approve' : 'approval.deny', {
        approval_id: current.id,
        scope: yes ? scope : 'once',
        match: yes ? match : 'arguments',
      })
      setQueue((items) => resolveApproval(items, current.id))
      setScope('once')
      setMatch('arguments')
    } finally {
      setBusy(false)
    }
  }
  if (!current)
    return (
      <main className="approval-window empty-window">
        <CheckCircle2 />
        <strong>没有待处理的电脑操作</strong>
        <p>窗口可以安全关闭。</p>
      </main>
    )
  return (
    <main className="approval-window">
      <header>
        <span>
          <ShieldAlert size={19} />
        </span>
        <div>
          <strong>允许 KoreAgent Next 操作这台电脑？</strong>
          <small>{queue.length > 1 ? `另有 ${queue.length - 1} 项等待处理` : '请检查本次操作'}</small>
        </div>
      </header>
      <section>
        <h1>{approvalTitle(current)}</h1>
        <p>{current.reason}</p>
        <p>授权只在所选匹配范围内生效；选择“所有参数”会跳过之后同类操作的审批。</p>
        <code>{current.tool_name}</code>
        <dl className="approval-facts">
          {approvalFacts(current.arguments).map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
        <details>
          <summary>查看原始参数</summary>
          <pre>{summary}</pre>
        </details>
      </section>
      <footer>
        <ApprovalControls
          approval={current}
          scope={scope}
          match={match}
          onScopeChange={setScope}
          onMatchChange={setMatch}
          disabled={busy}
        />
        <button disabled={busy} onClick={() => decide(false)}>
          <XCircle size={15} />
          拒绝
        </button>
        <button className="primary" disabled={busy} onClick={() => decide(true)}>
          <CheckCircle2 size={15} />
          允许
        </button>
      </footer>
    </main>
  )
}
