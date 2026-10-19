import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Bot,
  ChevronRight,
  FileOutput,
  FolderOpen,
  Menu,
  MessageSquare,
  PauseCircle,
  Pencil,
  Pin,
  PinOff,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from 'lucide-react'
import { artifactContentUrl } from '../apiConfig'
import { ApprovalControls } from '../ApprovalControls'
import { approvalTitle } from '../approvalPresentation'
import { useConfirmAction } from '../ConfirmDialog'
import { reduceAgentEvent, type LiveRunState } from '../liveState'
import { rpc } from '../rpc'
import type {
  Approval,
  Artifact,
  Conversation,
  Message,
  PermissionMatch,
  PermissionScope,
  Run,
  Task,
} from '../types'
import { Busy, Empty, ErrorBox, Status, openUrl, statusChinese } from '../ui'

const starterPrompts = [
  '梳理这个项目并给出可执行计划',
  '检查当前实现的风险与缺口',
  '完成一个任务并把结果发布为交付物',
]
export function Chat({
  initialConversationId,
  requestedMode,
  onOpenRun,
}: {
  initialConversationId: string | null
  requestedMode: 'normal' | 'plan' | null
  onOpenRun(id: string): void
}) {
  const confirmAction = useConfirmAction()
  const qc = useQueryClient()
  const [selected, setSelected] = useState('')
  const [input, setInput] = useState('')
  const [mode, setMode] = useState<'normal' | 'plan'>('normal')
  const [runWorkspace, setRunWorkspace] = useState<string | null>(null)
  const [live, setLive] = useState<LiveRunState | null>(null)
  const liveRef = useRef<LiveRunState | null>(null)
  useEffect(() => {
    liveRef.current = live
  }, [live])
  const [planTaskId, setPlanTaskId] = useState<string | null>(null)
  const [drawer, setDrawer] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [pinned, setPinned] = useState<string[]>(() =>
    JSON.parse(localStorage.getItem('koreagent:pinned-conversations') || '[]'),
  )
  useEffect(() => {
    if (initialConversationId) setSelected(initialConversationId)
  }, [initialConversationId])
  useEffect(() => {
    setLive(null)
    setRunWorkspace(null)
  }, [selected])
  useEffect(() => {
    if (requestedMode) setMode(requestedMode)
  }, [requestedMode])
  const conversations = useQuery({
    queryKey: ['conversations'],
    queryFn: () =>
      rpc
        .call<{ conversations: Conversation[] }>('conversation.list', { limit: 100 })
        .then((x) => x.conversations),
  })
  const ordered = useMemo(
    () =>
      [...(conversations.data || [])].sort(
        (a, b) => Number(pinned.includes(b.id)) - Number(pinned.includes(a.id)),
      ),
    [conversations.data, pinned],
  )
  useEffect(() => {
    if (!selected && ordered[0]) setSelected(initialConversationId || ordered[0].id)
  }, [ordered, selected, initialConversationId])
  const detail = useQuery({
    queryKey: ['conversation', selected],
    enabled: !!selected,
    queryFn: () =>
      rpc.call<{ conversation: Conversation; messages: Message[] }>('conversation.get', {
        conversation_id: selected,
      }),
  })
  const conversationWorkspace = detail.data?.conversation.workspace_path || null
  const tasks = useQuery({
    queryKey: ['tasks', selected],
    enabled: !!selected,
    queryFn: () =>
      rpc
        .call<{ tasks: Task[] }>('task.list', { conversation_id: selected, limit: 100 })
        .then((x) => x.tasks),
  })
  const runs = useQuery({
    queryKey: ['runs', selected],
    enabled: !!selected,
    queryFn: () =>
      rpc.call<{ runs: Run[] }>('run.list', { conversation_id: selected, limit: 20 }).then((x) => x.runs),
  })
  const catchUpRun = useCallback(async (runId: string, afterSeq = 0) => {
    try {
      const result = await rpc.call<{ events: any[] }>('run.events', {
        run_id: runId,
        after_seq: afterSeq,
        limit: 2000,
      })
      if (result.events.length)
        setLive((previous) =>
          result.events.reduce((state, event) => reduceAgentEvent(state, event), previous),
        )
    } catch {
      // The next reconnect or run-list refresh retries the catch-up.
    }
  }, [])
  useEffect(() => {
    if (!selected || liveRef.current?.runId) return
    const active = runs.data?.find(
      (item) => item.conversation_id === selected && ['running', 'pending'].includes(item.status),
    )
    if (active) void catchUpRun(active.id)
  }, [runs.data, selected, catchUpRun])
  useEffect(() => {
    return rpc.onStatusChange((status) => {
      if (status !== 'connected') return
      const active = liveRef.current
      if (active?.runId) void catchUpRun(active.runId, active.lastSeq || 0)
    })
  }, [catchUpRun])
  const artifacts = useQuery({
    queryKey: ['artifacts', selected],
    enabled: !!selected,
    queryFn: () =>
      rpc
        .call<{ artifacts: Artifact[] }>('artifact.list', { conversation_id: selected, limit: 50 })
        .then((x) => x.artifacts),
  })
  const create = useMutation({
    mutationFn: () => rpc.call<{ conversation: Conversation }>('conversation.create', {}),
    onSuccess: (r) => {
      setSelected(r.conversation.id)
      setDrawer(false)
      qc.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
  const remove = useMutation({
    mutationFn: (id: string) => rpc.call('conversation.delete', { conversation_id: id }),
    onSuccess: (_result, id) => {
      qc.setQueryData<Conversation[]>(['conversations'], (items) => items?.filter((item) => item.id !== id))
      setSelected('')
      qc.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
  const rename = useMutation({
    mutationFn: (title: string) => rpc.call('conversation.rename', { conversation_id: selected, title }),
    onSuccess: () => {
      setRenaming(false)
      qc.invalidateQueries({ queryKey: ['conversation', selected] })
      qc.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
  const approvals = useQuery({
    queryKey: ['approvals', 'chat'],
    queryFn: () =>
      rpc
        .call<{ approvals: Approval[] }>('approval.list', { status: 'pending', limit: 50 })
        .then((x) => x.approvals),
    refetchInterval: 15000,
  })
  const send = useMutation({
    mutationFn: ({ id, text, workspacePath }: { id: string; text: string; workspacePath: string | null }) =>
      rpc.call<any>('conversation.send', {
        conversation_id: id,
        content: text,
        mode,
        ...(workspacePath ? { workspace_path: workspacePath } : {}),
      }),
    onMutate: () => {
      setLive({ runId: '', content: '', reasoning: '', tools: [], toolActivity: [], status: 'starting' })
      setPlanTaskId(null)
    },
    onSuccess: async (result) => {
      setInput('')
      setPlanTaskId(result.plan_task_id || null)
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['conversation', selected] }),
        qc.invalidateQueries({ queryKey: ['conversations'] }),
        qc.invalidateQueries({ queryKey: ['tasks', selected] }),
        qc.invalidateQueries({ queryKey: ['artifacts', selected] }),
      ])
      setLive(null)
      setRunWorkspace(null)
    },
    onError: () => setLive(null),
  })
  const workspace = useMutation({
    mutationFn: (workspacePath: string | null) =>
      rpc.call('conversation.set_workspace', {
        conversation_id: selected,
        workspace_path: workspacePath,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['conversation', selected] })
      qc.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
  useEffect(
    () =>
      rpc.on('agent.event', (event: any) => {
        if (event?.conversation_id === selected) setLive((previous) => reduceAgentEvent(previous, event))
      }),
    [selected],
  )
  const pendingApproval = approvals.data?.find((item) => !live?.runId || item.run_id === live.runId)
  const approval = useMutation({
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
    onSuccess: () => approvals.refetch(),
  })
  const interrupt = useMutation({
    mutationFn: (id: string) => rpc.call('run.interrupt', { run_id: id }),
    onSuccess: () => setLive((previous) => (previous ? { ...previous, status: 'interrupted' } : null)),
  })
  const recover = useMutation({
    mutationFn: ({ id, workspacePath }: { id: string; workspacePath?: string | null }) =>
      rpc.call('run.recover', {
        run_id: id,
        ...(workspacePath ? { workspace_path: workspacePath } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['runs', selected] })
      qc.invalidateQueries({ queryKey: ['conversation', selected] })
    },
  })
  const resolveTool = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'retry' | 'abandon' }) =>
      rpc.call('run.resolve_tool', { run_id: id, action }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['runs', selected] })
      qc.invalidateQueries({ queryKey: ['conversation', selected] })
    },
  })
  const resolveUnknownTool = (id: string, action: 'retry' | 'abandon') => {
    if (action === 'abandon') {
      resolveTool.mutate({ id, action })
      return
    }
    void confirmAction({
      title: '重新执行这个工具？',
      message: '上一次工具调用可能已经产生副作用。只有确认可以安全重试时才继续。',
      confirmLabel: '确认重试',
    }).then((yes) => {
      if (yes) resolveTool.mutate({ id, action })
    })
  }
  const plan = useQuery({
    queryKey: ['task', planTaskId],
    enabled: !!planTaskId,
    queryFn: () => rpc.call<{ task: Task }>('task.get', { task_id: planTaskId }).then((x) => x.task),
  })
  const decidePlan = useMutation({
    mutationFn: (accepted: boolean) =>
      rpc.call<{ task: Task }>(accepted ? 'task.plan_accept' : 'task.plan_reject', { task_id: planTaskId }),
    onSuccess: (result) => {
      qc.setQueryData(['task', planTaskId], result.task)
      qc.invalidateQueries({ queryKey: ['tasks', selected] })
    },
  })
  const submit = () => {
    const text = input.trim()
    if (selected && text && !send.isPending) send.mutate({ id: selected, text, workspacePath: runWorkspace })
  }
  const pickWorkspace = async () => {
    if (window.koreNext?.chooseWorkspace) return window.koreNext.chooseWorkspace()
    return window.prompt('输入工作区绝对路径')?.trim() || null
  }
  const bindWorkspace = async () => {
    const path = await pickWorkspace()
    if (path) workspace.mutate(path)
  }
  const chooseRunWorkspace = async () => {
    const path = await pickWorkspace()
    if (path) setRunWorkspace(path)
  }
  const recoverRun = async (id: string) => {
    try {
      await recover.mutateAsync({ id })
    } catch (error: any) {
      if (error?.data?.code !== 'workspace_unavailable') return
      const path = await pickWorkspace()
      if (path) recover.mutate({ id, workspacePath: path })
    }
  }
  const pin = (id: string) => {
    const next = pinned.includes(id) ? pinned.filter((item) => item !== id) : [...pinned, id]
    setPinned(next)
    localStorage.setItem('koreagent:pinned-conversations', JSON.stringify(next))
  }
  const currentRun = runs.data?.find((item) => ['running', 'pending'].includes(item.status)) || runs.data?.[0]
  const activeTask =
    tasks.data?.find((item) => ['active', 'blocked', 'pending'].includes(item.status)) || tasks.data?.[0]
  return (
    <div className="chat-layout">
      {drawer && (
        <button className="drawer-scrim" aria-label="关闭会话列表" onClick={() => setDrawer(false)} />
      )}
      <aside className={`conversation-list ${drawer ? 'open' : ''}`} aria-label="会话列表">
        <div className="conversation-title">
          <strong>会话</strong>
          <div>
            <button aria-label="新建会话" onClick={() => create.mutate()}>
              <Plus size={16} />
            </button>
            <button className="mobile-only" aria-label="关闭会话列表" onClick={() => setDrawer(false)}>
              <X size={16} />
            </button>
          </div>
        </div>
        <div className="conversation-scroll">
          {ordered.map((item) => (
            <div className={`conversation-row ${selected === item.id ? 'selected' : ''}`} key={item.id}>
              <button
                aria-current={selected === item.id ? 'true' : undefined}
                onClick={() => {
                  setSelected(item.id)
                  setDrawer(false)
                }}
              >
                <span>{item.title}</span>
                <small>{item.message_count} 条消息</small>
              </button>
              <button
                aria-label={pinned.includes(item.id) ? '取消置顶' : '置顶会话'}
                title={pinned.includes(item.id) ? '取消置顶' : '置顶会话'}
                onClick={() => pin(item.id)}
              >
                {pinned.includes(item.id) ? <PinOff size={13} /> : <Pin size={13} />}
              </button>
            </div>
          ))}
        </div>
      </aside>
      <section className="workspace">
        {!selected ? (
          <>
            <button className="mobile-only empty-menu" onClick={() => setDrawer(true)}>
              <Menu size={16} />
              会话列表
            </button>
            <Empty
              icon={MessageSquare}
              title="开始新对话"
              hint="创建会话，然后告诉 KoreAgent Next 你想完成的结果。"
              action={
                <button className="primary" onClick={() => create.mutate()}>
                  <Plus size={15} />
                  新建会话
                </button>
              }
            />
          </>
        ) : (
          <>
            <header className="workspace-head">
              <button className="mobile-only icon" aria-label="打开会话列表" onClick={() => setDrawer(true)}>
                <Menu size={18} />
              </button>
              <div>
                {renaming ? (
                  <RenameForm
                    initial={detail.data?.conversation.title || ''}
                    save={(value) => rename.mutate(value)}
                    cancel={() => setRenaming(false)}
                  />
                ) : (
                  <>
                    <h2>{detail.data?.conversation.title || '会话'}</h2>
                    <small>
                      {send.isPending
                        ? 'Agent 正在工作'
                        : `${tasks.data?.filter((item) => ['active', 'blocked'].includes(item.status)).length || 0} 个持续任务`}
                    </small>
                    <div className="workspace-line">
                      <span
                        className="workspace-tag"
                        title={conversationWorkspace || '仅聊天，不使用文件工具'}
                      >
                        <FolderOpen size={12} />
                        {conversationWorkspace ? workspaceName(conversationWorkspace) : '未绑定工作区'}
                      </span>
                      <button
                        type="button"
                        onClick={() => void bindWorkspace()}
                        disabled={workspace.isPending}
                      >
                        {conversationWorkspace ? '更换' : '绑定目录'}
                      </button>
                      {conversationWorkspace && (
                        <button
                          type="button"
                          onClick={() => workspace.mutate(null)}
                          disabled={workspace.isPending}
                        >
                          解除
                        </button>
                      )}
                    </div>
                  </>
                )}
              </div>
              <div className="row-actions">
                <button
                  className="icon"
                  aria-label="重命名会话"
                  title="重命名"
                  onClick={() => setRenaming(true)}
                >
                  <Pencil size={15} />
                </button>
                <button
                  className="icon danger"
                  aria-label="删除会话"
                  title="删除会话"
                  onClick={() => {
                    void confirmAction({
                      title: '删除这个会话？',
                      message: '会话及其消息将被删除，此操作无法撤销。',
                      confirmLabel: '删除会话',
                      destructive: true,
                    }).then((yes) => {
                      if (yes) remove.mutate(selected)
                    })
                  }}
                >
                  <Trash2 size={16} />
                </button>
              </div>
            </header>
            <TaskPanel
              task={activeTask}
              run={currentRun}
              onOpenRun={onOpenRun}
              recovering={recover.isPending}
              onRecover={(id) => void recoverRun(id)}
              resolving={resolveTool.isPending}
              onResolveTool={resolveUnknownTool}
            />
            {tasks.error && (
              <div className="chat-query-error" role="alert">
                任务进度加载失败。<button onClick={() => tasks.refetch()}>重试</button>
              </div>
            )}
            <div className="messages" aria-live="polite">
              {detail.isLoading ? (
                <Busy />
              ) : detail.error ? (
                <div className="chat-query-error" role="alert">
                  会话加载失败。<button onClick={() => detail.refetch()}>重试</button>
                </div>
              ) : detail.data?.messages.length ? (
                detail.data.messages.map((message, index) => <MessageBubble key={index} message={message} />)
              ) : (
                <div className="starter-state">
                  <Empty icon={Bot} title="空白会话" hint="选择一个起点，或直接输入你的目标。" />
                  <div className="starter-prompts">
                    {starterPrompts.map((prompt) => (
                      <button key={prompt} onClick={() => setInput(prompt)}>
                        {prompt}
                        <ChevronRight size={14} />
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {live && <LiveRun live={live} />}{' '}
              {plan.data && (
                <PlanDecision
                  task={plan.data}
                  pending={decidePlan.isPending}
                  decide={(accepted) => decidePlan.mutate(accepted)}
                />
              )}{' '}
              {artifacts.data?.map((item) => (
                <ArtifactInline key={item.id} artifact={item} />
              ))}
            </div>
            {pendingApproval && (
              <InlineApproval
                approval={pendingApproval}
                pending={approval.isPending}
                decide={(yes, scope, match) => approval.mutate({ id: pendingApproval.id, yes, scope, match })}
              />
            )}{' '}
            {send.error && <ErrorBox error={send.error} />}{' '}
            {workspace.error && <ErrorBox error={workspace.error} />}{' '}
            {recover.error && <ErrorBox error={recover.error} />}
            {resolveTool.error && <ErrorBox error={resolveTool.error} />}
            <div className="composer">
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    submit()
                  }
                }}
                placeholder="输入消息；Enter 发送，Shift+Enter 换行"
                aria-label="消息内容"
              />
              <div className="composer-row">
                <div className="composer-actions">
                  <select
                    aria-label="运行模式"
                    value={mode}
                    onChange={(e) => setMode(e.target.value as 'normal' | 'plan')}
                  >
                    <option value="normal">执行模式</option>
                    <option value="plan">计划模式</option>
                  </select>
                  <button
                    type="button"
                    className={`workspace-run ${runWorkspace ? 'active' : ''}`}
                    title={runWorkspace || '仅本次运行使用另一个目录'}
                    onClick={() => void chooseRunWorkspace()}
                  >
                    <FolderOpen size={13} />
                    {runWorkspace ? workspaceName(runWorkspace) : '仅本次目录'}
                  </button>
                  {runWorkspace && (
                    <button
                      type="button"
                      className="icon"
                      aria-label="取消本次目录"
                      onClick={() => setRunWorkspace(null)}
                    >
                      <X size={13} />
                    </button>
                  )}
                </div>
                <button className="primary" disabled={!input.trim() || send.isPending} onClick={submit}>
                  {send.isPending ? '运行中' : '发送'}
                </button>
              </div>
            </div>
            {live?.runId && live.status === 'running' && (
              <button
                className="floating-stop"
                disabled={interrupt.isPending}
                onClick={() => {
                  void confirmAction({
                    title: '中断当前运行？',
                    message: '当前执行将停止，已有进度会保存为检查点，可稍后恢复。',
                    confirmLabel: '中断并保存',
                  }).then((yes) => {
                    if (yes) interrupt.mutate(live.runId)
                  })
                }}
              >
                <PauseCircle size={15} />
                中断并保存检查点
              </button>
            )}
          </>
        )}
      </section>
    </div>
  )
}
function TaskPanel({
  task,
  run,
  onOpenRun,
  onRecover,
  recovering,
  onResolveTool,
  resolving,
}: {
  task?: Task
  run?: Run
  onOpenRun(id: string): void
  onRecover(id: string): void
  recovering: boolean
  onResolveTool(id: string, action: 'retry' | 'abandon'): void
  resolving: boolean
}) {
  if (!task && !run) return null
  const done = task?.steps.filter((step) => step.status === 'completed').length || 0
  const total = task?.steps.length || 0
  const current =
    task?.steps.find((step) => ['active', 'running', 'blocked'].includes(step.status)) ||
    task?.steps.find((step) => step.status === 'pending')
  return (
    <section className="chat-task-panel" aria-label="当前任务进度">
      <div className="chat-task-heading">
        <div>
          <strong>{task?.title || '当前运行'}</strong>
          <small>
            {task
              ? `${done}/${total} 步已完成${current ? ` · 当前：${current.title}` : ''}`
              : `状态：${statusChinese(run?.status || '未知')}`}
          </small>
        </div>
        <div className="row-actions">
          {run && (
            <button onClick={() => onOpenRun(run.id)}>
              查看运行 <ChevronRight size={14} />
            </button>
          )}
          {run && run.stop_reason === 'tool_unknown' && (
            <>
              <button disabled={resolving} onClick={() => onResolveTool(run.id, 'retry')}>
                <RefreshCw size={14} />
                确认重试
              </button>
              <button disabled={resolving} onClick={() => onResolveTool(run.id, 'abandon')}>
                放弃并修复
              </button>
            </>
          )}
          {run &&
            run.stop_reason !== 'tool_unknown' &&
            ['interrupted', 'failed', 'cancelled'].includes(run.status) && (
              <button disabled={recovering} onClick={() => onRecover(run.id)}>
                <RefreshCw size={14} />
                恢复
              </button>
            )}
        </div>
      </div>
      {run?.stop_reason === 'tool_unknown' && (
        <p className="chat-task-note">
          工具执行结果未知，系统已停止自动恢复。请确认是否可以安全重试，或放弃并保留一条可继续对话的占位结果。
        </p>
      )}
      {task && total > 0 && (
        <>
          <progress max={total} value={done} aria-label="任务完成进度" />
          <ol>
            {task.steps.map((step) => (
              <li key={step.id}>
                <Status value={step.status} />
                <span>{step.title}</span>
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  )
}
function workspaceName(path: string) {
  return (
    path
      .replace(/[\\/]+$/, '')
      .split(/[\\/]/)
      .pop() || path
  )
}

function RenameForm({
  initial,
  save,
  cancel,
}: {
  initial: string
  save(value: string): void
  cancel(): void
}) {
  const [value, setValue] = useState(initial)
  return (
    <form
      className="rename-form"
      onSubmit={(e) => {
        e.preventDefault()
        if (value.trim()) save(value.trim())
      }}
    >
      <input autoFocus value={value} onChange={(e) => setValue(e.target.value)} aria-label="会话标题" />
      <button className="primary" type="submit">
        保存
      </button>
      <button type="button" onClick={cancel}>
        取消
      </button>
    </form>
  )
}
function MessageBubble({ message }: { message: Message }) {
  return (
    <article className={`message ${message.role}`}>
      <div className="avatar">
        {message.role === 'user' ? '你' : message.role === 'assistant' ? 'K' : 'T'}
      </div>
      <div className="message-body">
        {message.name && <small>{message.name}</small>}
        {message.reasoning && (
          <details>
            <summary>思考过程</summary>
            <pre>{message.reasoning}</pre>
          </details>
        )}
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content || ''}</ReactMarkdown>
      </div>
    </article>
  )
}
function LiveRun({ live }: { live: LiveRunState }) {
  return (
    <article className="message assistant live-message">
      <div className="avatar">K</div>
      <div className="message-body">
        {live.reasoning && (
          <details>
            <summary>正在推理</summary>
            <pre>{live.reasoning}</pre>
          </details>
        )}
        {live.content ? (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{live.content}</ReactMarkdown>
        ) : (
          <div className="thinking">
            <span />
            <span />
            <span /> 正在组织下一步
          </div>
        )}
        {live.budgetStatus && <div className="budget-notice">运行预算：{live.budgetStatus}</div>}
        {live.toolActivity.length > 0 && (
          <div className="tool-activity">
            {live.toolActivity.map((tool) => (
              <div key={tool.id}>
                <Status value={tool.status} />
                <span>{tool.name}</span>
                {tool.summary && <small>{tool.summary}</small>}
              </div>
            ))}
          </div>
        )}
      </div>
    </article>
  )
}
function ArtifactInline({ artifact }: { artifact: Artifact }) {
  return (
    <article className="artifact-inline">
      <FileOutput size={18} />
      <div>
        <strong>{artifact.title}</strong>
        <small>{artifact.description || artifact.filename || 'Agent 交付物'}</small>
      </div>
      {artifact.kind === 'file' ? (
        <a className="button" href={artifactContentUrl(artifact.id)}>
          下载
        </a>
      ) : (
        <button onClick={() => artifact.source_url && openUrl(artifact.source_url)}>打开</button>
      )}
    </article>
  )
}
function InlineApproval({
  approval,
  pending,
  decide,
}: {
  approval: Approval
  pending: boolean
  decide(yes: boolean, scope: PermissionScope, match: PermissionMatch): void
}) {
  const [scope, setScope] = useState<PermissionScope>('once')
  const [match, setMatch] = useState<PermissionMatch>('arguments')
  return (
    <section className="inline-approval" role="alert">
      <div>
        <strong>等待允许：{approvalTitle(approval)}</strong>
        <p>{approval.reason}</p>
        <small>授权只在所选匹配范围内生效；选择“所有参数”会跳过之后同类操作的审批。</small>
      </div>
      <div>
        <ApprovalControls
          approval={approval}
          scope={scope}
          match={match}
          onScopeChange={setScope}
          onMatchChange={setMatch}
          disabled={pending}
        />
        <button disabled={pending} onClick={() => decide(false, 'once', 'arguments')}>
          拒绝
        </button>
        <button className="primary" disabled={pending} onClick={() => decide(true, scope, match)}>
          允许
        </button>
      </div>
    </section>
  )
}
function PlanDecision({
  task,
  pending,
  decide,
}: {
  task: Task
  pending: boolean
  decide(accepted: boolean): void
}) {
  return (
    <section className="plan-decision">
      <header>
        <div>
          <strong>{task.title}</strong>
          <small>计划需要确认后才会进入执行状态</small>
        </div>
        <Status value={task.status} />
      </header>
      <ol>
        {task.steps.map((step) => (
          <li key={step.id}>
            <span>{step.title}</span>
            {step.description && <small>{step.description}</small>}
          </li>
        ))}
      </ol>
      {task.status === 'pending' && (
        <footer>
          <button disabled={pending} onClick={() => decide(false)}>
            退回计划
          </button>
          <button className="primary" disabled={pending} onClick={() => decide(true)}>
            接受计划
          </button>
        </footer>
      )}
    </section>
  )
}
