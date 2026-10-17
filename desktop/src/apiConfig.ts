const defaultRpcUrl = 'ws://127.0.0.1:8765/rpc'

export function rpcUrl(): string {
  return import.meta.env.VITE_RPC_URL || defaultRpcUrl
}

export function httpBaseUrl(value = rpcUrl()): string {
  const url = new URL(value)
  url.protocol = url.protocol === 'wss:' ? 'https:' : 'http:'
  url.pathname = url.pathname.replace(/\/rpc\/?$/, '') || '/'
  url.search = ''
  url.hash = ''
  return url.toString().replace(/\/$/, '')
}

export function artifactContentUrl(id: string): string {
  return `${httpBaseUrl()}/artifacts/${encodeURIComponent(id)}/content`
}

export function computerScreenshotUrl(id: string): string {
  return `${httpBaseUrl()}/computer/screenshots/${encodeURIComponent(id)}.png`
}
