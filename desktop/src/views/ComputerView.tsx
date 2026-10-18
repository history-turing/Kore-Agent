import { useMutation, useQuery } from '@tanstack/react-query'
import { Cpu } from 'lucide-react'
import { computerScreenshotUrl } from '../apiConfig'
import { rpc } from '../rpc'
import { Busy, Empty, ErrorBox, Page, Status } from '../ui'

export function Computer() {
  const status = useQuery({
    queryKey: ['computer'],
    queryFn: () => rpc.call<any>('computer.status', {}),
    refetchInterval: 15000,
  })
  const observation = useQuery({
    queryKey: ['observation'],
    queryFn: () => rpc.call<any>('computer.latest_observation', {}),
    refetchInterval: 10000,
  })
  const permission = useMutation({
    mutationFn: (name: string) => rpc.call('computer.request_permission', { permission: name }),
    onSuccess: () => status.refetch(),
  })
  return (
    <Page title="电脑" subtitle="查看 macOS Computer Helper 的状态、权限、租约和最新观察。">
      <div className="computer-grid">
        <article className="card">
          <Cpu size={24} />
          <h3>Computer Runtime</h3>
          {status.isLoading ? (
            <Busy />
          ) : (
            <>
              <Status value={status.data?.available ? 'available' : 'unavailable'} />
              <dl>
                <dt>平台</dt>
                <dd>{status.data?.platform}</dd>
                <dt>运行时</dt>
                <dd>{status.data?.runtime || '—'}</dd>
                <dt>辅助功能</dt>
                <dd>{status.data?.permissions?.accessibility}</dd>
                <dt>屏幕录制</dt>
                <dd>{status.data?.permissions?.screen_recording}</dd>
                <dt>机器租约</dt>
                <dd>
                  {status.data?.lease?.busy ? `Run ${status.data.lease.owner_run_id?.slice(0, 8)}` : '空闲'}
                </dd>
              </dl>
              {status.data?.platform === 'darwin' && (
                <div className="permission-actions">
                  <button
                    disabled={permission.isPending || status.data?.permissions?.accessibility === 'granted'}
                    onClick={() => permission.mutate('accessibility')}
                  >
                    请求辅助功能权限
                  </button>
                  <button
                    disabled={
                      permission.isPending || status.data?.permissions?.screen_recording === 'granted'
                    }
                    onClick={() => permission.mutate('screen_recording')}
                  >
                    请求屏幕录制权限
                  </button>
                </div>
              )}
              <p>{status.data?.reason}</p>
              {permission.error && <ErrorBox error={permission.error} />}
            </>
          )}
        </article>
        <article className="observation">
          <h3>最新观察</h3>
          {observation.data?.observation ? (
            <>
              <img
                alt="Computer Helper 最新桌面观察"
                src={computerScreenshotUrl(observation.data.observation.id)}
              />
              <div className="observation-summary">
                <span>{observation.data.observation.elements?.length || 0} 个界面元素</span>
                <span>{observation.data.observation.windows?.length || 0} 个窗口</span>
              </div>
              <details>
                <summary>查看元素树数据</summary>
                <pre>{JSON.stringify(observation.data.observation, null, 2)}</pre>
              </details>
            </>
          ) : (
            <Empty icon={Cpu} title="没有观察数据" hint="运行电脑控制任务后会显示截图和元素树。" />
          )}
        </article>
      </div>
    </Page>
  )
}
