import { useQuery } from '@tanstack/react-query'
import { ExternalLink, FileOutput } from 'lucide-react'
import { artifactContentUrl } from '../apiConfig'
import { rpc } from '../rpc'
import type { Artifact } from '../types'
import { Empty, ErrorBox, Page, formatBytes, openUrl } from '../ui'

export function Artifacts() {
  const query = useQuery({
    queryKey: ['artifacts'],
    queryFn: () =>
      rpc.call<{ artifacts: Artifact[] }>('artifact.list', { limit: 100 }).then((x) => x.artifacts),
  })
  return (
    <Page title="交付物" subtitle="查看 Agent 发布的文件和链接。">
      <div className="card-grid">
        {query.data?.map((item) => (
          <article className="card" key={item.id}>
            <FileOutput />
            <h3>{item.title}</h3>
            <p>{item.description || item.filename || item.source_url}</p>
            <small>
              {item.kind === 'file' ? formatBytes(item.size_bytes) : '外部链接'} ·{' '}
              {new Date(item.created_at).toLocaleString()}
            </small>
            {item.kind === 'url' && item.source_url ? (
              <button className="primary" onClick={() => openUrl(item.source_url!)}>
                <ExternalLink size={14} />
                打开链接
              </button>
            ) : (
              <a className="button primary" href={artifactContentUrl(item.id)}>
                下载文件
              </a>
            )}
          </article>
        ))}
      </div>
      {query.error && <ErrorBox error={query.error} />}{' '}
      {!query.isLoading && !query.data?.length && (
        <Empty icon={FileOutput} title="暂无交付物" hint="Agent 发布的文件和链接会显示在这里。" />
      )}
    </Page>
  )
}
