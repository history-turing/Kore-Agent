import type { Approval, PermissionScope } from './types'

const toolLabels: Record<string, string> = {
  computer_click: '点击界面元素',
  computer_type: '向应用输入文字',
  computer_key: '发送键盘按键',
  computer_open_app: '打开应用',
  computer_focus_window: '切换窗口',
  write_file: '写入工作区文件',
  shell_execute: '运行命令',
}

const scopeLabels: Record<PermissionScope, string> = {
  once: '仅本次',
  run: '本次运行',
  conversation: '当前会话',
  global: '始终允许',
}

export function approvalTitle(approval: Pick<Approval, 'tool_name'|'arguments'>): string {
  const base = toolLabels[approval.tool_name] || approval.tool_name.replaceAll('_', ' ')
  const target = approval.arguments.path || approval.arguments.app || approval.arguments.host || approval.arguments.url_origin
  return target ? `${base}：${String(target)}` : base
}

export function approvalFacts(argumentsValue: Record<string, unknown>): Array<[string, string]> {
  const friendly: Record<string, string> = { path:'路径', app:'应用', host:'主机', method:'方式', text:'输入内容', key:'按键', url:'网址', url_origin:'来源', server_name:'服务' }
  return Object.entries(argumentsValue)
    .filter(([,value]) => ['string','number','boolean'].includes(typeof value))
    .slice(0, 8)
    .map(([key,value]) => [friendly[key] || key, key === 'text' ? redactText(String(value)) : String(value)])
}

export function permissionScopeLabel(scope: PermissionScope): string {
  return scopeLabels[scope]
}

function redactText(value: string): string {
  if (value.length <= 80) return value
  return `${value.slice(0, 77)}…`
}
