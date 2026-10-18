import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Archive,
  Bot,
  Brain,
  Clock3,
  Cpu,
  FileOutput,
  MessageSquare,
  Search,
  Settings,
  ShieldCheck,
  X,
  Zap,
} from 'lucide-react'
import { queryKeysForAgentEvent } from './eventInvalidation'
import { ConfirmProvider, useConfirmAction } from './ConfirmDialog'
import PreferencesView from './PreferencesView'
import RunExplorer from './RunExplorer'
import { rpc } from './rpc'
import type { Approval, Conversation, Run } from './types'
import { Approvals } from './views/ApprovalsView'
import { Artifacts } from './views/ArtifactsView'
import { Automations } from './views/AutomationsView'
import { Chat } from './views/ChatView'
import { Computer } from './views/ComputerView'
import { Memory } from './views/MemoryView'

type View = 'chat' | 'runs' | 'automations' | 'approvals' | 'artifacts' | 'computer' | 'memory' | 'settings'
const nav: Array<[View, string, typeof MessageSquare]> = [
  ['chat', '对话', MessageSquare],
  ['runs', '运行', Archive],
  ['automations', '自动化', Clock3],
  ['approvals', '审批', ShieldCheck],
  ['artifacts', '交付物', FileOutput],
  ['computer', '电脑', Cpu],
  ['memory', '记忆', Brain],
  ['settings', '设置', Settings],
]

type Toast = { id: number; message: string; tone: 'success' | 'danger' | 'info' }
type Command = { id: string; label: string; hint: string; run(): void }
function useHostEvents(pushToast: (message: string, tone: Toast['tone']) => void) {
  const query = useQueryClient()
  useEffect(() => {
    rpc.connect()
    // Durable events invalidate only the views they affect; token deltas are
    // rendered by the live reducer without repeatedly fetching the full trace.
    const invalidate = (...keys: string[]) =>
      keys.forEach((key) => query.invalidateQueries({ queryKey: [key] }))
    const agent = rpc.on('agent.event', (event: any) => {
      const kind = String(event?.type || '')
      invalidate(...queryKeysForAgentEvent(kind))
      if (kind === 'agent_completed') {
        window.koreNext?.notify('KoreAgent Next', '任务已完成')
        pushToast('任务已完成', 'success')
      }
      if (kind === 'agent_failed') {
        window.koreNext?.notify('KoreAgent Next', '任务执行失败，请查看运行记录')
        pushToast('任务执行失败，请查看运行记录', 'danger')
      }
    })
    const run = rpc.on('run.status', () => invalidate('runs'))
    const required = rpc.on('approval.required', (approval: any) => {
      invalidate('approvals')
      if (approval?.ui_scope === 'desktop' || approval?.tool_name?.startsWith('computer_'))
        window.koreNext?.showApproval()
    })
    const resolved = rpc.on('approval.resolved', () => invalidate('approvals'))
    const artifact = rpc.on('artifact.created', (item: any) => {
      invalidate('artifacts')
      const message = `已生成交付物：${item?.title || '未命名'}`
      window.koreNext?.notify('KoreAgent Next', message)
      pushToast(message, 'success')
    })
    return () => {
      agent()
      run()
      required()
      resolved()
      artifact()
    }
  }, [pushToast, query])
}

export default function App() {
  return (
    <ConfirmProvider>
      <AppContent />
    </ConfirmProvider>
  )
}

function AppContent() {
  const confirmAction = useConfirmAction()
  const [view, setView] = useState<View>('chat')
  const [status, setStatus] = useState(rpc.status)
  const [palette, setPalette] = useState(false)
  const [hadConnection, setHadConnection] = useState(false)
  const [toasts, setToasts] = useState<Toast[]>([])
  const [focusedConversationId, setFocusedConversationId] = useState<string | null>(null)
  const [focusedRunId, setFocusedRunId] = useState<string | null>(null)
  const [requestedMode, setRequestedMode] = useState<'normal' | 'plan' | null>(null)
  const pushToast = useCallback((message: string, tone: Toast['tone']) => {
    const id = Date.now() + Math.random()
    setToasts((items) => [...items.slice(-3), { id, message, tone }])
    window.setTimeout(() => setToasts((items) => items.filter((item) => item.id !== id)), 5000)
  }, [])
  useHostEvents(pushToast)
  const runSummary = useQuery({
    queryKey: ['runs', 'rail'],
    queryFn: () => rpc.call<{ runs: Run[] }>('run.list', { limit: 100 }).then((value) => value.runs),
    refetchInterval: 15000,
    retry: false,
  })
  const approvalSummary = useQuery({
    queryKey: ['approvals', 'rail'],
    queryFn: () =>
      rpc
        .call<{ approvals: Approval[] }>('approval.list', { status: 'pending', limit: 100 })
        .then((value) => value.approvals),
    refetchInterval: 15000,
    retry: false,
  })
  const runningCount =
    runSummary.data?.filter((item) => ['pending', 'running'].includes(item.status)).length || 0
  const approvalCount = approvalSummary.data?.length || 0
  const activeRun = runSummary.data?.find((item) => ['pending', 'running'].includes(item.status))
  const commands: Command[] = [
    {
      id: 'new-conversation',
      label: '新建会话',
      hint: '对话',
      run: () => {
        void rpc
          .call<{ conversation: Conversation }>('conversation.create', {})
          .then((result) => {
            setFocusedConversationId(result.conversation.id)
            setView('chat')
          })
          .catch((error) => pushToast(String(error), 'danger'))
      },
    },
    {
      id: 'mode-normal',
      label: '切换为执行模式',
      hint: '对话',
      run: () => {
        setRequestedMode('normal')
        setView('chat')
      },
    },
    {
      id: 'mode-plan',
      label: '切换为计划模式',
      hint: '对话',
      run: () => {
        setRequestedMode('plan')
        setView('chat')
      },
    },
    ...(activeRun
      ? [
          {
            id: 'view-active-run',
            label: '查看当前运行',
            hint: activeRun.user_message.slice(0, 40),
            run: () => {
              setFocusedRunId(activeRun.id)
              setView('runs')
            },
          },
          {
            id: 'stop-active-run',
            label: '中断当前运行',
            hint: '保留检查点',
            run: () => {
              void confirmAction({
                title: '中断当前运行？',
                message: '当前执行将停止，已有进度会保存为检查点，可稍后恢复。',
                confirmLabel: '中断并保存',
              }).then((yes) => {
                if (yes)
                  void rpc
                    .call('run.interrupt', { run_id: activeRun.id })
                    .catch((error) => pushToast(String(error), 'danger'))
              })
            },
          },
        ]
      : []),
    ...nav.map(([id, label]): Command => ({
      id: `go-${id}`,
      label: `打开${label}`,
      hint: '页面',
      run: () => setView(id),
    })),
  ]
  useEffect(() => {
    rpc.onStatus = (value) => {
      setStatus(value)
      if (value === 'connected') setHadConnection(true)
    }
    rpc.connect()
    return () => {
      rpc.onStatus = undefined
    }
  }, [])
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (document.querySelector('[role="alertdialog"]')) return
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setPalette((value) => !value)
      }
      if (event.key === 'Escape') setPalette(false)
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [])
  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="主导航">
        <div className="brand" title="KoreAgent Next">
          <Bot size={20} />
        </div>
        <nav>
          {nav.map(([id, label, Icon]) => {
            const count = id === 'runs' ? runningCount : id === 'approvals' ? approvalCount : 0
            return (
              <button
                key={id}
                aria-label={label}
                aria-current={view === id ? 'page' : undefined}
                title={label}
                className={view === id ? 'active' : ''}
                onClick={() => setView(id)}
              >
                <Icon size={19} />
                <span>{label}</span>
                {count > 0 && (
                  <b className="nav-badge" aria-label={`${count} 项待处理`}>
                    {count > 99 ? '99+' : count}
                  </b>
                )}
              </button>
            )
          })}
        </nav>
        <button
          className="palette-trigger"
          aria-label="打开操作面板"
          title="操作面板（Ctrl+K）"
          onClick={() => setPalette(true)}
        >
          <Search size={17} />
          <span>操作</span>
        </button>
        <div
          className={`host-dot ${status}`}
          role="status"
          aria-label={`Host ${status}`}
          title={`Host: ${status}`}
        />
      </aside>
      <main>
        {hadConnection && status === 'offline' && (
          <div className="host-banner" role="status">
            <span />
            Host 连接已断开，正在重连…
          </div>
        )}
        {view === 'chat' ? (
          <Chat
            initialConversationId={focusedConversationId}
            requestedMode={requestedMode}
            onOpenRun={(id) => {
              setFocusedRunId(id)
              setView('runs')
            }}
          />
        ) : view === 'runs' ? (
          <RunExplorer
            initialRunId={focusedRunId}
            onOpenConversation={(id) => {
              setFocusedConversationId(id)
              setView('chat')
            }}
          />
        ) : view === 'automations' ? (
          <Automations />
        ) : view === 'approvals' ? (
          <Approvals />
        ) : view === 'artifacts' ? (
          <Artifacts />
        ) : view === 'computer' ? (
          <Computer />
        ) : view === 'memory' ? (
          <Memory />
        ) : (
          <PreferencesView />
        )}
      </main>
      {palette && <CommandPalette commands={commands} close={() => setPalette(false)} />}
      <ToastTray
        items={toasts}
        dismiss={(id) => setToasts((values) => values.filter((item) => item.id !== id))}
      />
    </div>
  )
}

function CommandPalette({ commands, close }: { commands: Command[]; close(): void }) {
  const [term, setTerm] = useState('')
  const [focused, setFocused] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const previousFocus = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null)
  useEffect(() => {
    input.current?.focus()
    return () => previousFocus.current?.focus()
  }, [])
  const items = commands.filter((item) =>
    `${item.label} ${item.hint}`.toLowerCase().includes(term.trim().toLowerCase()),
  )
  useEffect(() => setFocused(0), [term])
  const execute = (item: Command) => {
    item.run()
    close()
  }
  const key = (event: React.KeyboardEvent) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setFocused((value) => (value + 1) % Math.max(1, items.length))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setFocused((value) => (value - 1 + items.length) % Math.max(1, items.length))
    } else if (event.key === 'Enter' && items[focused]) {
      event.preventDefault()
      execute(items[focused])
    } else if (event.key === 'Escape') {
      event.preventDefault()
      close()
    } else if (event.key === 'Tab') {
      const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('input,button'))
      const first = controls[0],
        last = controls.at(-1)
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last?.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first?.focus()
      }
    }
  }
  return (
    <div
      className="modal-back"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close()
      }}
    >
      <section
        className="command-palette"
        role="dialog"
        aria-modal="true"
        aria-labelledby="command-title"
        onKeyDown={key}
      >
        <h2 id="command-title">操作面板</h2>
        <label className="search-field">
          <Search size={16} />
          <input
            ref={input}
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder="搜索页面或操作"
            aria-label="搜索页面或操作"
          />
        </label>
        <div role="listbox">
          {items.map((item, index) => (
            <button
              role="option"
              aria-selected={focused === index}
              key={item.id}
              className={focused === index ? 'active' : ''}
              onMouseEnter={() => setFocused(index)}
              onClick={() => execute(item)}
            >
              <Zap size={17} />
              <span>{item.label}</span>
              <small>{item.hint}</small>
            </button>
          ))}
          {items.length === 0 && <p className="palette-empty">没有匹配的操作</p>}
        </div>
        <footer>
          <span>↑ ↓ 选择</span>
          <span>Enter 执行</span>
        </footer>
        <button className="close-palette" aria-label="关闭" onClick={close}>
          <X size={16} />
        </button>
      </section>
    </div>
  )
}
function ToastTray({ items, dismiss }: { items: Toast[]; dismiss(id: number): void }) {
  return (
    <div className="toast-viewport" aria-live="polite">
      {items.map((item) => (
        <div className={`toast toast-${item.tone}`} role="status" key={item.id}>
          <span>{item.message}</span>
          <button aria-label="关闭通知" onClick={() => dismiss(item.id)}>
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  )
}
