import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { CheckCircle2, ShieldCheck, XCircle } from 'lucide-react'
import { ApprovalControls } from '../ApprovalControls'
import {
  approvalFacts,
  approvalTitle,
  permissionMatchLabel,
  permissionScopeLabel,
} from '../approvalPresentation'
import { rpc } from '../rpc'
import type { Approval, PermissionMatch, PermissionScope } from '../types'
import { Empty, ErrorBox, Page, Status } from '../ui'

export function Approvals() {
  const query = useQuery({
    queryKey: ['approvals'],
    queryFn: () =>
      rpc.call<{ approvals: Approval[] }>('approval.list', { limit: 100 }).then((x) => x.approvals),
    refetchInterval: 15000,
  })
  const [scopes, setScopes] = useState<Record<string, PermissionScope>>({})
  const [matches, setMatches] = useState<Record<string, PermissionMatch>>({})
  const action = useMutation({
    mutationFn: ({
      id,
      yes,
      scope,
      match,
    }: {
      id: string
      yes: boolean
      scope: PermissionScope
      match: PermissionMatch
    }) => rpc.call(yes ? 'approval.approve' : 'approval.deny', { approval_id: id, scope, match }),
    onSuccess: () => query.refetch(),
  })
  return (
    <Page title="审批" subtitle="用可读信息审查敏感操作，并选择权限持续范围。">
      <div className="stack">
        {query.data?.map((item) => (
          <article className="approval" key={item.id}>
            <div>
              <div className="approval-heading">
                <Status value={item.status} />
                <code>{item.tool_name}</code>
              </div>
              <h3>{approvalTitle(item)}</h3>
              <p>{item.reason}</p>
              <small>
                Run {item.run_id?.slice(0, 8) || '—'} · {new Date(item.created_at).toLocaleString()}
                {item.resolution_scope ? ` · ${permissionScopeLabel(item.resolution_scope)}` : ''}
                {item.resolution_match ? ` · ${permissionMatchLabel(item.resolution_match)}` : ''}
              </small>
              <dl className="approval-facts">
                {approvalFacts(item.arguments).map(([key, value]) => (
                  <div key={key}>
                    <dt>{key}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
              <details>
                <summary>查看原始参数</summary>
                <pre>{JSON.stringify(item.arguments, null, 2)}</pre>
              </details>
            </div>
            {item.status === 'pending' && (
              <div className="approval-actions">
                <ApprovalControls
                  approval={item}
                  scope={scopes[item.id] || 'once'}
                  match={matches[item.id] || 'arguments'}
                  onScopeChange={(scope) => setScopes({ ...scopes, [item.id]: scope })}
                  onMatchChange={(match) => setMatches({ ...matches, [item.id]: match })}
                  disabled={action.isPending}
                />
                <button
                  className="danger"
                  disabled={action.isPending}
                  onClick={() =>
                    action.mutate({ id: item.id, yes: false, scope: 'once', match: 'arguments' })
                  }
                >
                  <XCircle size={15} />
                  拒绝
                </button>
                <button
                  className="primary"
                  disabled={action.isPending}
                  onClick={() =>
                    action.mutate({
                      id: item.id,
                      yes: true,
                      scope: scopes[item.id] || 'once',
                      match: matches[item.id] || 'arguments',
                    })
                  }
                >
                  <CheckCircle2 size={15} />
                  允许
                </button>
              </div>
            )}
          </article>
        ))}
      </div>
      {query.error && <ErrorBox error={query.error} />}{' '}
      {!query.isLoading && !query.data?.length && (
        <Empty icon={ShieldCheck} title="没有审批请求" hint="需要确认的工具操作会出现在这里。" />
      )}
    </Page>
  )
}
