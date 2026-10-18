import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Brain } from 'lucide-react'
import { rpc } from '../rpc'
import type { MemoryItem, Task } from '../types'
import { Empty, Page, Status } from '../ui'

export function Memory() {
  const query = useQuery({ queryKey: ['memory'], queryFn: () => rpc.call<any>('memory.list', {}) })
  const tasks = useQuery({
    queryKey: ['tasks'],
    queryFn: () => rpc.call<{ tasks: Task[] }>('task.list', { limit: 100 }).then((x) => x.tasks),
  })
  const [tab, setTab] = useState<'active' | 'archived'>('active')
  const memories: MemoryItem[] = query.data?.[tab] || []
  return (
    <Page title="记忆" subtitle="查看核心事实、活跃与归档的长期记忆，以及持续任务。">
      <div className="memory-layout">
        <section>
          <div className="memory-title">
            <h2>核心记忆</h2>
            <small>
              最近维护：
              {query.data?.maintenance?.last_run_at
                ? new Date(query.data.maintenance.last_run_at).toLocaleString()
                : '尚未运行'}
            </small>
          </div>
          {query.data?.core_entries?.length ? (
            <dl className="core-entries">
              {query.data.core_entries.map((entry: any) => (
                <div key={entry.key}>
                  <dt>{entry.key}</dt>
                  <dd>
                    {entry.value}
                    <small>修订 {entry.revision}</small>
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            <div className="core-memory">尚未形成核心记忆。</div>
          )}
          <div className="memory-tabs">
            <button className={tab === 'active' ? 'active' : ''} onClick={() => setTab('active')}>
              活跃 {query.data?.active_count || 0}
            </button>
            <button className={tab === 'archived' ? 'active' : ''} onClick={() => setTab('archived')}>
              归档 {query.data?.archived?.length || 0}
            </button>
          </div>
          <div className="stack">
            {memories.map((item) => (
              <article className="memory-card" key={item.id}>
                <Status value={item.status} />
                <h3>{item.title}</h3>
                <p>{item.summary}</p>
                <small>
                  访问 {item.access_count} 次 · 修订 {item.revision} ·{' '}
                  {new Date(item.updated_at).toLocaleString()}
                </small>
              </article>
            ))}
          </div>
          {!memories.length && (
            <Empty
              icon={Brain}
              title={tab === 'active' ? '没有活跃记忆' : '没有归档记忆'}
              hint="稳定偏好与长期事实会在运行结束后经过反思写入。"
            />
          )}
        </section>
        <section>
          <h2>持续任务</h2>
          <div className="stack">
            {tasks.data?.map((task) => (
              <article className="memory-card" key={task.id}>
                <Status value={task.status} />
                <h3>{task.title}</h3>
                <p>{task.description || task.goal}</p>
                <ol className="task-steps">
                  {task.steps.map((step) => (
                    <li key={step.id}>
                      <Status value={step.status} />
                      {step.title}
                    </li>
                  ))}
                </ol>
                <small>{new Date(task.updated_at).toLocaleString()}</small>
              </article>
            ))}
          </div>
          {!tasks.data?.length && <Empty title="暂无持续任务" hint="Agent 创建的计划和任务会显示在这里。" />}
        </section>
      </div>
    </Page>
  )
}
