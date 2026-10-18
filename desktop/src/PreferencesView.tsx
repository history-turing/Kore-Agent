import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { RefreshCw, RotateCcw } from 'lucide-react'
import { rpc } from './rpc'
import { Status } from './ui'
import { ExtensionManager } from './views/ExtensionManager'

function PreferencesView() {
  const info = useQuery({ queryKey: ['system'], queryFn: () => rpc.call<any>('system.info') })
  const settings = useQuery({
    queryKey: ['model-settings'],
    queryFn: () => rpc.call<any>('model_settings.get'),
  })
  const extensions = useQuery({ queryKey: ['extensions'], queryFn: () => rpc.call<any>('extension.list') })
  const [provider, setProvider] = useState('openai')
  const current = useMemo(
    () => settings.data?.providers?.find((p: any) => p.provider === provider),
    [settings.data, provider],
  )
  const [draft, setDraft] = useState<any>({})
  const [roles, setRoles] = useState<any>({})
  useEffect(() => {
    if (current) setDraft({ ...current, api_key: '' })
  }, [current])
  useEffect(() => {
    if (settings.data)
      setRoles({
        reflection: settings.data.reflection,
        maintenance: settings.data.maintenance,
        summary: settings.data.summary,
        skill_learning: settings.data.skill_learning,
      })
  }, [settings.data])

  const input = () => ({
    provider,
    model: draft.model,
    base_url: draft.base_url || null,
    api_style: draft.api_style,
    api_key: draft.api_key || undefined,
  })
  const save = useMutation({
    mutationFn: () =>
      rpc.call('model_settings.update', {
        default_provider: provider,
        providers: settings.data.providers.map((p: any) =>
          p.provider === provider
            ? input()
            : { provider: p.provider, model: p.model, base_url: p.base_url, api_style: p.api_style },
        ),
        reflection: roles.reflection,
        maintenance: roles.maintenance,
        summary: roles.summary,
        skill_learning: roles.skill_learning,
      }),
    onSuccess: () => settings.refetch(),
  })
  const test = useMutation({ mutationFn: () => rpc.call<any>('model_settings.test', input()) })
  const restart = useMutation({
    mutationFn: () => rpc.call('system.restart', {}),
    onSuccess: () => {
      settings.refetch()
      extensions.refetch()
    },
  })

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h1>设置</h1>
          <p>配置模型服务、运行环境和扩展能力。</p>
        </div>
      </header>
      <div className="page-body">
        <div className="settings-grid">
          <section className="settings-section">
            <div className="section-heading">
              <h2>Host</h2>
              {settings.data?.restart_required && <Status value="restart_required" />}
            </div>
            <dl>
              <dt>版本</dt>
              <dd>{info.data?.version}</dd>
              <dt>数据库</dt>
              <dd className="mono">{info.data?.database}</dd>
              <dt>默认工作区</dt>
              <dd className="mono">{info.data?.workspace}</dd>
              <dt>RPC 方法</dt>
              <dd>{info.data?.rpc_methods?.length || 0}</dd>
              <dt>工作区沙箱</dt>
              <dd>
                {info.data?.execution_capabilities?.workspace_sandbox === 'unavailable'
                  ? '不可用：Shell 与默认 MCP 将拒绝启动'
                  : info.data?.execution_capabilities?.workspace_sandbox || '未知'}
              </dd>
            </dl>
            {info.data?.legacy_permission_rules_needing_review > 0 && (
              <p className="field-note">
                有 {info.data.legacy_permission_rules_needing_review}{' '}
                条旧授权规则已停用；再次执行相应操作时需要重新审批。
              </p>
            )}
            {settings.data?.restart_required && (
              <div className="restart-panel">
                <p>扩展配置已改变，需要重启 Host 内部服务才能生效。</p>
                <button
                  disabled={!settings.data?.can_restart || restart.isPending}
                  onClick={() => restart.mutate()}
                >
                  <RotateCcw size={14} />
                  {restart.isPending ? '重启中' : '安全重启服务'}
                </button>
                {!settings.data?.can_restart && <small>有运行中的任务，完成或中断后才能重启。</small>}
              </div>
            )}
            {restart.error && <p className="form-error">{String(restart.error)}</p>}
          </section>
          <section className="settings-section">
            <h2>模型</h2>
            <div className="provider-tabs">
              {settings.data?.providers.map((p: any) => (
                <button
                  className={provider === p.provider ? 'active' : ''}
                  key={p.provider}
                  onClick={() => setProvider(p.provider)}
                >
                  {p.label}
                </button>
              ))}
            </div>
            {current && (
              <div className="form-grid">
                <label>
                  模型
                  <input
                    value={draft.model || ''}
                    onChange={(e) => setDraft({ ...draft, model: e.target.value })}
                  />
                </label>
                <label>
                  Base URL
                  <input
                    value={draft.base_url || ''}
                    onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
                  />
                </label>
                <label>
                  API 风格
                  <select
                    value={draft.api_style || 'chat_completions'}
                    onChange={(e) => setDraft({ ...draft, api_style: e.target.value })}
                  >
                    <option value="chat_completions">Chat Completions</option>
                    <option value="responses">Responses</option>
                    <option value="anthropic_messages">Anthropic Messages</option>
                  </select>
                </label>
                <label>
                  API Key
                  <input
                    type="password"
                    value={draft.api_key || ''}
                    placeholder={current.configured ? '已配置（留空保持）' : '请输入 API Key'}
                    onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
                  />
                </label>
                <p className="field-note">
                  密钥来源：
                  {current.key_source === 'dpapi'
                    ? 'Windows DPAPI'
                    : current.key_source === 'keychain'
                      ? 'macOS Keychain'
                      : current.key_source === 'environment'
                        ? '环境变量'
                        : '未配置'}
                </p>
                <div className="inline-actions">
                  <button onClick={() => test.mutate()}>
                    {test.isPending ? <RefreshCw size={14} /> : null}测试连接
                  </button>
                  <button className="primary" onClick={() => save.mutate()}>
                    保存并设为默认
                  </button>
                </div>
                {test.data && (
                  <>
                    <Status value={test.data.success ? 'available' : 'failed'} />
                    {test.data.error && <p className="form-error">{test.data.error}</p>}
                  </>
                )}{' '}
                {save.error && <p className="form-error">{String(save.error)}</p>}
              </div>
            )}
            <div className="role-settings">
              <h3>后台模型角色</h3>
              {(['reflection', 'maintenance', 'summary', 'skill_learning'] as const).map((role) => {
                const value = roles[role] || {}
                return (
                  <div className="role-row" key={role}>
                    <span>
                      <strong>
                        {
                          {
                            reflection: '运行后反思',
                            maintenance: '记忆维护',
                            summary: '上下文摘要',
                            skill_learning: '技能学习',
                          }[role]
                        }
                      </strong>
                      <small>
                        实际使用：{settings.data?.active_roles?.[role]?.provider || '—'} /{' '}
                        {settings.data?.active_roles?.[role]?.model || '—'}
                      </small>
                    </span>
                    <label>
                      <input
                        type="checkbox"
                        checked={value.enabled ?? true}
                        onChange={(e) =>
                          setRoles({ ...roles, [role]: { ...value, enabled: e.target.checked } })
                        }
                      />
                      启用
                    </label>
                    <label>
                      <input
                        type="checkbox"
                        checked={value.inherit_main ?? true}
                        onChange={(e) =>
                          setRoles({ ...roles, [role]: { ...value, inherit_main: e.target.checked } })
                        }
                      />
                      继承主模型
                    </label>
                    {value.inherit_main === false && (
                      <div className="role-override">
                        <select
                          aria-label={`${role} Provider`}
                          value={value.provider || provider}
                          onChange={(e) =>
                            setRoles({ ...roles, [role]: { ...value, provider: e.target.value } })
                          }
                        >
                          {settings.data?.providers.map((item: any) => (
                            <option key={item.provider} value={item.provider}>
                              {item.label}
                            </option>
                          ))}
                        </select>
                        <input
                          aria-label={`${role} 模型`}
                          placeholder="模型名"
                          value={value.model || ''}
                          onChange={(e) =>
                            setRoles({ ...roles, [role]: { ...value, model: e.target.value } })
                          }
                        />
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </section>
          <section className="settings-section span-2">
            <ExtensionManager data={extensions.data} refresh={() => extensions.refetch()} />
          </section>
        </div>
      </div>
    </section>
  )
}

export default PreferencesView
