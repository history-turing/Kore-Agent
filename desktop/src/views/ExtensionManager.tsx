import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { AlertTriangle, Plus, Trash2 } from 'lucide-react'
import { useConfirmAction } from '../ConfirmDialog'
import { rpc } from '../rpc'
import { Status } from '../ui'

export function ExtensionManager({ data, refresh }: { data: any; refresh(): unknown }) {
  const confirmAction = useConfirmAction()
  const [skill, setSkill] = useState({ name: '', description: '', instructions: '', scope: 'project' })
  const [mcp, setMcp] = useState({
    name: '',
    command: '',
    args: '',
    cwd: '',
    env: '',
    permission: 'human_approval',
    filesystem: 'workspace',
    network: 'denied',
    readable_roots: '',
    writable_roots: '',
    allowed_domains: '',
  })
  const [source, setSource] = useState('')
  const [importPlan, setImportPlan] = useState<any>(null)
  const install = useMutation({
    mutationFn: () => rpc.call('skill.install', skill),
    onSuccess: () => {
      setSkill({ ...skill, name: '', description: '', instructions: '' })
      refresh()
    },
  })
  const toggle = useMutation({
    mutationFn: ({ item, enabled }: any) =>
      rpc.call('skill.set_enabled', { name: item.name, scope: item.scope, enabled }),
    onSuccess: refresh,
  })
  const deleteSkill = useMutation({
    mutationFn: (item: any) =>
      rpc.call('skill.delete', { name: item.name, scope: item.scope, enabled: item.enabled }),
    onSuccess: refresh,
  })
  const env = Object.fromEntries(
    mcp.env
      .split(/\r?\n/)
      .map((line) => line.split('=', 2).map((part) => part.trim()))
      .filter((parts) => parts.length === 2 && parts[0]),
  )
  const roots = (value: string) =>
    value
      .split(/\r?\n|,/)
      .map((item) => item.trim())
      .filter(Boolean)
  const mcpPayload = {
    name: mcp.name,
    command: mcp.command,
    args: mcp.args.split(/\s+/).filter(Boolean),
    cwd: mcp.cwd || null,
    env,
    enabled: true,
    permission: mcp.permission,
    sandbox: {
      filesystem: mcp.filesystem,
      network: mcp.network,
      readable_roots: roots(mcp.readable_roots),
      writable_roots: roots(mcp.writable_roots),
      allowed_domains: roots(mcp.allowed_domains),
    },
  }
  const addMcp = useMutation({
    mutationFn: () => rpc.call('mcp.add', mcpPayload),
    onSuccess: () => {
      setMcp({ ...mcp, name: '', command: '', args: '', cwd: '', env: '' })
      refresh()
    },
  })
  const toggleMcp = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      rpc.call('mcp.set_enabled', { name, enabled }),
    onSuccess: refresh,
  })
  const deleteMcp = useMutation({
    mutationFn: (name: string) => rpc.call('mcp.delete', { name }),
    onSuccess: refresh,
  })
  const previewExtension = useMutation({
    mutationFn: () =>
      rpc.call<any>('extension.import.preview', {
        input: source,
        skill_scope: 'project',
        mcp_permission: 'human_approval',
      }),
    onSuccess: (result) => setImportPlan(result.plan),
  })
  const importExtension = useMutation({
    mutationFn: () =>
      rpc.call('extension.import.apply', {
        input: source,
        skill_scope: 'project',
        mcp_permission: 'human_approval',
        fingerprint: importPlan.fingerprint,
        confirmed: true,
      }),
    onSuccess: () => {
      setSource('')
      setImportPlan(null)
      refresh()
    },
  })
  const promote = useMutation({
    mutationFn: (candidate: any) =>
      rpc.call('skill.install', {
        name: candidate.name,
        description: candidate.description,
        instructions: candidate.instructions,
        scope: 'project',
        candidate_id: candidate.id,
      }),
    onSuccess: refresh,
  })
  return (
    <>
      <h2>扩展</h2>
      <div className="import-row">
        <input
          value={source}
          onChange={(e) => {
            setSource(e.target.value)
            setImportPlan(null)
          }}
          placeholder="扩展路径、GitHub URL 或 mcpServers JSON"
        />
        <button disabled={!source || previewExtension.isPending} onClick={() => previewExtension.mutate()}>
          <Plus size={14} />
          预览导入
        </button>
      </div>
      {importPlan && (
        <div className="import-plan">
          <strong>将执行 {importPlan.actions?.length || 0} 项变更</strong>
          <ul>
            {importPlan.actions?.map((action: string) => (
              <li key={action}>{action}</li>
            ))}
          </ul>
          {importPlan.warnings?.map((warning: string) => (
            <p key={warning}>{warning}</p>
          ))}
          <div className="row-actions">
            <button onClick={() => setImportPlan(null)}>取消</button>
            <button
              className="primary"
              disabled={importExtension.isPending}
              onClick={() => importExtension.mutate()}
            >
              确认并导入
            </button>
          </div>
        </div>
      )}
      {(previewExtension.error || importExtension.error) && (
        <p className="form-error">{String(previewExtension.error || importExtension.error)}</p>
      )}
      {data?.skill_diagnostics?.length > 0 && (
        <div className="diagnostic-list">
          <AlertTriangle size={17} />
          <div>
            <strong>Skill 诊断</strong>
            {data.skill_diagnostics.map((item: any) => (
              <p key={`${item.scope}:${item.name}`}>
                {item.name}：{item.error}
              </p>
            ))}
          </div>
        </div>
      )}
      <div className="extension-columns">
        <div>
          <h3>Skills</h3>
          {data?.skills?.map((s: any) => (
            <div className="extension-row" key={`${s.scope}:${s.name}`}>
              <div>
                <strong>{s.name}</strong>
                <small>{s.description}</small>
              </div>
              <div className="row-actions">
                <button onClick={() => toggle.mutate({ item: s, enabled: !s.enabled })}>
                  {s.enabled ? '停用' : '启用'}
                </button>
                <button
                  className="danger"
                  aria-label={`删除 Skill ${s.name}`}
                  onClick={() => {
                    void confirmAction({
                      title: `删除 Skill “${s.name}”？`,
                      message: '这项 Skill 将从当前作用域移除，后续运行无法使用。',
                      confirmLabel: '删除 Skill',
                      destructive: true,
                    }).then((yes) => {
                      if (yes) deleteSkill.mutate(s)
                    })
                  }}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
          ))}
          <div className="mini-form">
            <input
              placeholder="Skill 名称"
              value={skill.name}
              onChange={(e) => setSkill({ ...skill, name: e.target.value })}
            />
            <input
              placeholder="说明"
              value={skill.description}
              onChange={(e) => setSkill({ ...skill, description: e.target.value })}
            />
            <textarea
              placeholder="Skill 指令"
              value={skill.instructions}
              onChange={(e) => setSkill({ ...skill, instructions: e.target.value })}
            />
            <button
              className="primary"
              disabled={!skill.name || !skill.instructions}
              onClick={() => install.mutate()}
            >
              安装 Skill
            </button>
          </div>
          {data?.skill_candidates?.length > 0 && (
            <div className="candidate-list">
              <h3>从成功任务学习</h3>
              {data.skill_candidates.map((candidate: any) => (
                <div className="candidate-row" key={candidate.id}>
                  <div>
                    <strong>{candidate.title}</strong>
                    <small>
                      {candidate.tool_sequence?.join(' → ')} · 成功 {candidate.success_count} 次
                    </small>
                  </div>
                  <div className="row-actions">
                    <Status value={candidate.status} />
                    {candidate.status === 'ready' && (
                      <button disabled={promote.isPending} onClick={() => promote.mutate(candidate)}>
                        审核后安装
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
        <div>
          <h3>MCP Servers</h3>
          {data?.mcp?.servers?.map((s: any) => (
            <div className="extension-row" key={s.name}>
              <div>
                <strong>{s.name}</strong>
                <small>
                  {s.command} {s.args?.join(' ')} · {s.sandbox_backend}
                </small>
                {s.error && <small className="form-error">{s.error}</small>}
                <small>
                  {s.tool_names?.length || 0} 个工具 · 重启 {s.restart_count || 0} 次
                </small>
              </div>
              <div className="row-actions">
                <Status value={s.state} />
                <button onClick={() => toggleMcp.mutate({ name: s.name, enabled: !s.enabled })}>
                  {s.enabled ? '停用' : '启用'}
                </button>
                <button
                  className="danger"
                  aria-label={`删除 MCP ${s.name}`}
                  onClick={() => {
                    void confirmAction({
                      title: `删除 MCP Server “${s.name}”？`,
                      message: '服务器配置将被删除，之后的运行无法调用它提供的工具。',
                      confirmLabel: '删除 Server',
                      destructive: true,
                    }).then((yes) => {
                      if (yes) deleteMcp.mutate(s.name)
                    })
                  }}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
          ))}
          <div className="mini-form">
            <input
              placeholder="Server 名称"
              value={mcp.name}
              onChange={(e) => setMcp({ ...mcp, name: e.target.value })}
            />
            <input
              placeholder="命令"
              value={mcp.command}
              onChange={(e) => setMcp({ ...mcp, command: e.target.value })}
            />
            <input
              placeholder="参数（空格分隔）"
              value={mcp.args}
              onChange={(e) => setMcp({ ...mcp, args: e.target.value })}
            />
            <input
              placeholder="工作目录（可选）"
              value={mcp.cwd}
              onChange={(e) => setMcp({ ...mcp, cwd: e.target.value })}
            />
            <textarea
              placeholder={'环境变量，每行 NAME=value'}
              value={mcp.env}
              onChange={(e) => setMcp({ ...mcp, env: e.target.value })}
            />
            <div className="mcp-options">
              <label>
                权限
                <select
                  value={mcp.permission}
                  onChange={(e) => setMcp({ ...mcp, permission: e.target.value })}
                >
                  <option value="human_approval">每次审批</option>
                  <option value="allowed">允许</option>
                  <option value="forbidden">禁止</option>
                </select>
              </label>
              <label>
                文件系统
                <select
                  value={mcp.filesystem}
                  onChange={(e) => setMcp({ ...mcp, filesystem: e.target.value })}
                >
                  <option value="workspace">仅工作区</option>
                  <option value="readonly">只读</option>
                  <option value="host">主机</option>
                </select>
              </label>
              <label>
                网络
                <select value={mcp.network} onChange={(e) => setMcp({ ...mcp, network: e.target.value })}>
                  <option value="denied">禁止</option>
                  <option value="allowed_domains">仅允许域名</option>
                  <option value="allowed">允许</option>
                </select>
              </label>
            </div>
            <textarea
              placeholder="可读目录（逗号或换行分隔）"
              value={mcp.readable_roots}
              onChange={(e) => setMcp({ ...mcp, readable_roots: e.target.value })}
            />
            <textarea
              placeholder="可写目录（逗号或换行分隔）"
              value={mcp.writable_roots}
              onChange={(e) => setMcp({ ...mcp, writable_roots: e.target.value })}
            />
            <textarea
              placeholder="允许域名（逗号或换行分隔）"
              value={mcp.allowed_domains}
              onChange={(e) => setMcp({ ...mcp, allowed_domains: e.target.value })}
            />
            <details className="json-preview">
              <summary>配置预览</summary>
              <pre>{JSON.stringify(mcpPayload, null, 2)}</pre>
            </details>
            <button className="primary" disabled={!mcp.name || !mcp.command} onClick={() => addMcp.mutate()}>
              添加 MCP
            </button>
          </div>
        </div>
      </div>
    </>
  )
}
