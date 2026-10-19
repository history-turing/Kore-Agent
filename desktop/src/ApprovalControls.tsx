import { permissionMatchLabel, permissionScopeLabel } from './approvalPresentation'
import type { Approval, PermissionMatch, PermissionScope } from './types'

export function ApprovalControls({
  approval,
  scope,
  match,
  onScopeChange,
  onMatchChange,
  disabled = false,
}: {
  approval: Approval
  scope: PermissionScope
  match: PermissionMatch
  onScopeChange(scope: PermissionScope): void
  onMatchChange(match: PermissionMatch): void
  disabled?: boolean
}) {
  const matches: PermissionMatch[] = approval.available_matches?.length
    ? approval.available_matches
    : ['arguments']
  const activeMatch = matches.includes(match) ? match : 'arguments'

  const changeScope = (next: PermissionScope) => {
    onScopeChange(next)
    if (next === 'once') onMatchChange('arguments')
  }

  return (
    <div className="approval-controls">
      <label className="approval-control">
        <span>范围</span>
        <select
          value={scope}
          onChange={(event) => changeScope(event.target.value as PermissionScope)}
          aria-label="允许范围"
          disabled={disabled}
        >
          {(approval.available_scopes || ['once']).map((item) => (
            <option value={item} key={item}>
              {permissionScopeLabel(item)}
            </option>
          ))}
        </select>
      </label>
      {scope !== 'once' && matches.length > 1 && (
        <label className="approval-control">
          <span>匹配</span>
          <select
            value={activeMatch}
            onChange={(event) => onMatchChange(event.target.value as PermissionMatch)}
            aria-label="匹配方式"
            disabled={disabled}
          >
            {matches.map((item) => (
              <option value={item} key={item}>
                {permissionMatchLabel(item)}
              </option>
            ))}
          </select>
        </label>
      )}
      {scope === 'global' && activeMatch === 'tool' && (
        <small className="approval-warning">
          高风险：该工具的所有参数都会被自动允许，之后同类操作不会再询问。
        </small>
      )}
    </div>
  )
}
