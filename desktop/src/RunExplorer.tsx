import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  Archive, ArrowLeft, ChevronRight, ExternalLink, FileOutput, Filter,
  MessageSquare, RefreshCw, RotateCcw, Square,
} from 'lucide-react'
import { artifactContentUrl } from './apiConfig'
import { activityEntries, contextSteps, eventCategory, eventTitle, formatTokens, traceStep } from './runInsights'
import { rpc } from './rpc'
import type { Artifact, ModelUsage, Run, RunTracePayload, TraceEvent } from './types'
import './runExplorer.css'

type RunFilter='all'|'active'|'attention'
type TraceFilter='all'|'model'|'tools'|'approval'|'context'|'memory'
const filters:Array<[TraceFilter,string]>=[
  ['all','全部'],['model','模型'],['tools','工具'],['approval','审批'],['context','上下文'],['memory','后台处理'],
]

function when(value:string|null|undefined):string {
  if(!value)return '—'
  const date=new Date(value)
  return Number.isNaN(date.getTime())?value:date.toLocaleString()
}

function elapsed(start:string|undefined,end:string|null|undefined):string {
  if(!start||!end)return '—'
  const ms=new Date(end).getTime()-new Date(start).getTime()
  if(!Number.isFinite(ms)||ms<0)return '—'
  return ms<60_000?`${(ms/1000).toFixed(1)} 秒`:`${Math.floor(ms/60_000)} 分 ${Math.round(ms%60_000/1000)} 秒`
}

function statusLabel(value:string):string {
  return ({pending:'等待中',running:'进行中',completed:'已完成',failed:'失败',interrupted:'已中断',cancelled:'已取消'} as Record<string,string>)[value]||value
}

function EmptyRun({title,hint}:{title:string;hint:string}){
  return <div className="run-empty"><Archive size={23}/><strong>{title}</strong><p>{hint}</p></div>
}

function Section({id,title,hint,children}:{id:string;title:string;hint?:string;children:React.ReactNode}){
  return <section id={id} className="run-section"><div className="run-section-head"><div><h3>{title}</h3>{hint&&<p>{hint}</p>}</div></div>{children}</section>
}

function UsageCell({label,usage}:{label:string;usage:ModelUsage|undefined}){
  return <div className="run-usage-cell"><span>{label}</span><strong>{usage?formatTokens(usage.total_tokens):'未提供'}</strong><small>{usage?`${usage.model_calls} 次调用 · 输入 ${formatTokens(usage.input_tokens)} · 输出 ${formatTokens(usage.output_tokens)}`:'等待运行记录'}</small></div>
}

function UsagePanel({data}:{data:RunTracePayload}){
  const summary=data.usage_summary
  if(!summary?.main_agent)return <p className="run-muted">当前 Host 尚未提供分类用量。</p>
  const main=summary.main_agent
  const auxiliary=[summary.context_summary,summary.memory_reflection,summary.memory_maintenance,summary.skill_learning]
  const auxiliaryCalls=auxiliary.reduce((count,usage)=>count+(usage?.model_calls||0),0)
  return <>
    <div className="run-usage-main">
      <div><span>主 Agent</span><strong>{formatTokens(main.total_tokens)} <small>tokens</small></strong><em>{main.model_calls} 次模型调用</em></div>
      <dl>
        <div><dt>输入</dt><dd>{formatTokens(main.input_tokens)}</dd></div>
        <div><dt>缓存输入</dt><dd>{formatTokens(main.cached_input_tokens)}</dd></div>
        <div><dt>未缓存输入</dt><dd>{formatTokens(main.uncached_input_tokens)}</dd></div>
        <div><dt>输出</dt><dd>{formatTokens(main.output_tokens)}</dd></div>
        <div><dt>缓存读取</dt><dd>{formatTokens(main.cache_read_input_tokens)}</dd></div>
        <div><dt>缓存写入</dt><dd>{formatTokens(main.cache_write_input_tokens)}</dd></div>
      </dl>
    </div>
    {main.cached_input_tokens===null&&<p className="run-footnote">提供商未返回缓存明细；“未提供”不代表 0。</p>}
    <div className="run-usage-grid">
      <UsageCell label="上下文摘要" usage={summary.context_summary}/>
      <UsageCell label="记忆反思" usage={summary.memory_reflection}/>
      <UsageCell label="记忆维护" usage={summary.memory_maintenance}/>
      <UsageCell label="技能学习" usage={summary.skill_learning}/>
    </div>
    <div className="run-total"><span>提供商总用量 <small>主 Agent + 辅助模型</small></span><strong>{formatTokens(summary.provider_total?.total_tokens)} tokens</strong></div>
    {auxiliaryCalls===0&&<p className="run-footnote">本次运行没有记录辅助模型调用。旧运行在升级前产生的辅助用量无法补算。</p>}
  </>
}

function BudgetPanel({data}:{data:RunTracePayload}){
  const budget=data.usage_summary?.budget
  if(!budget)return <p className="run-muted">暂无运行预算数据。</p>
  const used=budget.run_budget_chargeable_tokens
  const hard=Math.max(1,budget.run_budget_hard_tokens)
  const reason=budget.run_budget_reason==='model_calls'?'模型调用次数':budget.run_budget_reason==='tokens'?'Token 用量':null
  return <>
    <div className="run-budget-heading"><span className={`run-budget-state ${budget.run_budget_status}`}>{({normal:'正常',warning:'接近上限',finalizing:'收尾中',exceeded:'已超限'} as Record<string,string>)[budget.run_budget_status]||budget.run_budget_status}</span><strong>{formatTokens(used)} / {formatTokens(hard)} tokens</strong></div>
    <progress className="run-budget-progress" max={hard} value={Math.min(used,hard)} aria-label="运行 Token 预算占用"/>
    <dl className="run-budget-thresholds">
      <div><dt>提醒</dt><dd>{formatTokens(budget.run_budget_warning_tokens)} tokens · {budget.run_budget_warning_model_calls} 次调用</dd></div>
      <div><dt>收尾</dt><dd>{formatTokens(budget.run_budget_finalization_tokens)} tokens · {budget.run_budget_finalization_model_calls} 次调用</dd></div>
      <div><dt>上限</dt><dd>{formatTokens(budget.run_budget_hard_tokens)} tokens · {budget.run_budget_hard_model_calls} 次调用</dd></div>
    </dl>
    <p className="run-footnote">本次已调用 {budget.run_budget_model_calls} 次模型{reason?`；阈值由${reason}触发。`:'；尚未触发阈值。'}</p>
  </>
}

function ContextPanel({events}:{events:TraceEvent[]}){
  const steps=useMemo(()=>contextSteps(events),[events])
  const [selected,setSelected]=useState<number|null>(null)
  useEffect(()=>{if(steps.length&&!steps.some(item=>item.step===selected))setSelected(steps.at(-1)!.step)},[steps,selected])
  const current=steps.find(item=>item.step===selected)||steps.at(-1)
  if(!current)return <p className="run-muted">模型开始请求后，会显示各步骤的上下文构成。</p>
  const context=current.context
  const rows:Array<[string,number]>=[
    ['系统指令',context.breakdown.system],['会话消息',context.breakdown.messages],
    ['工具结果',context.breakdown.tool_results],['工具定义',context.breakdown.tool_schemas],
  ]
  const total=Math.max(1,context.prepared_input_tokens)
  return <>
    <div className="run-step-picker" aria-label="选择模型步骤"><span>模型步骤</span><div>{steps.map(item=><button key={item.step} aria-pressed={item.step===current.step} className={item.step===current.step?'active':''} onClick={()=>setSelected(item.step)}>{item.step}</button>)}</div></div>
    <div className="run-context-stats"><div><span>压缩前</span><strong>{formatTokens(context.original_estimated_tokens)}</strong></div><div><span>模型输入</span><strong>{formatTokens(context.prepared_input_tokens)}</strong></div><div><span>输入上限</span><strong>{formatTokens(context.max_input_tokens)}</strong></div><div><span>上下文窗口</span><strong>{formatTokens(context.context_window)}</strong></div></div>
    <div className="run-context-bars">{rows.map(([label,value])=><div key={label} className="run-context-bar"><span>{label}</span><progress max={total} value={Math.min(value,total)} aria-label={`${label}占用`}/><strong>{formatTokens(value)}</strong><small>{Math.round(value/total*100)}%</small></div>)}</div>
    <div className="run-context-note"><strong>{context.compacted?'已压缩上下文':'未触发历史压缩'}</strong><span>{context.compacted?`阶段 ${context.compaction_stage} · 移除 ${context.dropped_messages} 条消息${context.summary_updated?' · 摘要已更新':''}`:'当前步骤直接使用已准备的消息。'}</span><span>工具结果：{formatTokens(context.tool_result_tokens_before)} → {formatTokens(context.tool_result_tokens_after)} tokens</span></div>
    <p className="run-footnote">上下文和工具定义的 Token 数为本地估算；实际用量以提供商返回值为准。</p>
  </>
}

function ActivityPanel({events}:{events:TraceEvent[]}){
  const entries=useMemo(()=>activityEntries(events),[events])
  if(!entries.length)return <p className="run-muted">暂无活动记录。</p>
  return <ol className="run-activity">{entries.map(entry=><li key={entry.id} className={`run-activity-${entry.tone}`}><time>{when(entry.time)}</time><div><strong>{entry.title}</strong><p>{entry.description}</p></div>{entry.step>0&&<span>步骤 {entry.step}</span>}</li>)}</ol>
}

function ArtifactPanel({runId}:{runId:string}){
  const query=useQuery({queryKey:['artifacts','run',runId],queryFn:()=>rpc.call<{artifacts:Artifact[]}>('artifact.list',{run_id:runId,limit:500}).then(result=>result.artifacts)})
  if(query.isPending)return <p className="run-muted">正在读取交付物…</p>
  if(query.isError)return <div className="run-query-error" role="alert">交付物加载失败。<button onClick={()=>query.refetch()}>重试</button></div>
  if(!query.data?.length)return <p className="run-muted">当前 Run 没有发布交付物。</p>
  return <div className="run-artifacts">{query.data.map(artifact=><article key={artifact.id}><FileOutput size={18}/><div><strong>{artifact.title}</strong><small>{artifact.description||artifact.filename||when(artifact.created_at)}</small></div>{artifact.kind==='file'?<a className="button" href={artifactContentUrl(artifact.id)}>下载</a>:artifact.source_url?<button onClick={()=>openExternal(artifact.source_url!)}><ExternalLink size={14}/>打开</button>:null}</article>)}</div>
}

function openExternal(url:string):void {
  if(window.koreNext?.openExternal)window.koreNext.openExternal(url)
  else window.open(url,'_blank','noopener,noreferrer')
}

function TracePanel({events}:{events:TraceEvent[]}){
  const [filter,setFilter]=useState<TraceFilter>('all')
  const filtered=events.filter(event=>filter==='all'||eventCategory(event)===filter)
  const groups=new Map<number,TraceEvent[]>()
  for(const event of filtered){const step=traceStep(event);groups.set(step,[...(groups.get(step)||[]),event])}
  return <>
    <div className="run-trace-filters" aria-label="Trace 筛选"><Filter size={15}/>{filters.map(([id,label])=><button key={id} className={filter===id?'active':''} aria-pressed={filter===id} onClick={()=>setFilter(id)}>{label}</button>)}</div>
    {!filtered.length?<p className="run-muted">该筛选下没有事件。</p>:<div className="run-trace-groups">{[...groups].map(([step,items])=><section key={step}><h4>{step===0?'运行准备':`模型步骤 ${step}`}</h4>{items.map(event=><details key={event.event_id} className="run-raw-event"><summary><time>{new Date(event.event_time).toLocaleTimeString()}</time><strong>{eventTitle(event)}</strong><span>{event.tool_call?.name||event.tool_result?.tool_name||event.role||''}</span></summary><pre>{JSON.stringify(event,null,2)}</pre></details>)}</section>)}</div>}
  </>
}

export default function RunExplorer({onOpenConversation,initialRunId}:{onOpenConversation(id:string):void;initialRunId?:string|null}){
  const [selected,setSelected]=useState('')
  useEffect(()=>{if(initialRunId)setSelected(initialRunId)},[initialRunId])
  const [filter,setFilter]=useState<RunFilter>('all')
  const runs=useQuery({queryKey:['runs'],queryFn:()=>rpc.call<{runs:Run[]}>('run.list',{limit:200}).then(result=>result.runs),refetchInterval:15000})
  const visible=useMemo(()=>runs.data?.filter(run=>filter==='all'||(filter==='active'?['pending','running'].includes(run.status):['failed','interrupted','cancelled'].includes(run.status)))||[],[runs.data,filter])
  useEffect(()=>{if(!selected&&runs.data?.[0])setSelected(runs.data[0].id)},[runs.data,selected])
  const trace=useQuery({queryKey:['trace',selected],enabled:!!selected,queryFn:()=>rpc.call<RunTracePayload>('trace.get',{run_id:selected}),refetchInterval:query=>['pending','running'].includes(query.state.data?.trace.status||'')?2500:false})
  const interrupt=useMutation({mutationFn:(id:string)=>rpc.call('run.interrupt',{run_id:id}),onSuccess:()=>{runs.refetch();trace.refetch()}})
  const recover=useMutation({mutationFn:(id:string)=>rpc.call<{run:Run}>('run.recover',{run_id:id}),onSuccess:result=>{setSelected(result.run.id);runs.refetch()}})
  const current=trace.data
  const run=runs.data?.find(item=>item.id===selected)
  return <section className="page run-explorer"><header className="page-head"><div><h1>运行记录</h1><p>查看每次执行的用量、上下文、活动、交付物和完整事件。</p></div><button onClick={()=>{runs.refetch();trace.refetch()}}><RefreshCw size={15}/>刷新</button></header><div className="page-body run-explorer-body"><div className="run-toolbar" aria-label="运行筛选">{(['all','active','attention'] as RunFilter[]).map(item=><button key={item} className={filter===item?'active':''} aria-pressed={filter===item} onClick={()=>setFilter(item)}>{item==='all'?'全部':item==='active'?'进行中':'需处理'}</button>)}</div><div className="run-layout"><aside className="run-list" aria-label="运行列表">{runs.isPending?<p className="run-muted">正在读取运行记录…</p>:runs.isError?<div className="run-query-error" role="alert">运行列表加载失败。<button onClick={()=>runs.refetch()}>重试</button></div>:visible.length?visible.map(item=><button key={item.id} className={selected===item.id?'active':''} aria-current={selected===item.id?'true':undefined} onClick={()=>setSelected(item.id)}><span className={`run-state-dot ${item.status}`}/><span className="run-list-copy"><strong>{item.user_message||`Run ${item.id.slice(0,8)}`}</strong><small>{statusLabel(item.status)} · {when(item.created_at)}</small></span><ChevronRight size={15}/></button>):<EmptyRun title="没有匹配的运行" hint="切换筛选条件，或从对话中发起任务。"/>}</aside><div className="run-detail">{!selected?<EmptyRun title="选择一次运行" hint="查看完整执行信息。"/>:trace.isPending?<p className="run-muted">正在读取运行详情…</p>:trace.isError||!current?<div className="run-query-error" role="alert">运行详情加载失败。<button onClick={()=>trace.refetch()}>重试</button></div>:<>
    <div className="run-detail-header"><div><span className={`run-status ${current.trace.status}`}>{statusLabel(current.trace.status)}</span><h2>{run?.user_message||`Run ${selected.slice(0,8)}`}</h2><p>{current.trace.provider} / {current.trace.model} · 开始 {when(current.trace.started_at)} · 耗时 {elapsed(current.trace.started_at,current.trace.completed_at)}</p></div><div className="run-detail-actions">{current.trace.conversation_id&&<button onClick={()=>onOpenConversation(current.trace.conversation_id!)}><ArrowLeft size={15}/><MessageSquare size={15}/>返回会话</button>}{current.trace.status==='running'&&<button className="danger" disabled={interrupt.isPending} onClick={()=>interrupt.mutate(selected)}><Square size={14}/>中断</button>}{['failed','cancelled','interrupted'].includes(current.trace.status)&&<button disabled={recover.isPending} onClick={()=>recover.mutate(selected)}><RotateCcw size={14}/>恢复运行</button>}</div></div>
    {(run?.error||current.trace.stop_reason&&current.trace.stop_reason!=='completed')&&<div className="run-stop-note" role="status"><strong>结束原因：{current.trace.stop_reason||'错误'}</strong>{run?.error&&<p>{run.error}</p>}</div>}
    {(interrupt.error||recover.error)&&<div className="run-query-error" role="alert">{String(interrupt.error||recover.error)}</div>}
    <nav className="run-section-nav" aria-label="运行详情章节"><a href="#run-usage">用量</a><a href="#run-budget">预算</a><a href="#run-context">上下文</a><a href="#run-activity">活动</a><a href="#run-artifacts">交付物</a><a href="#run-trace">Trace</a></nav>
    <Section id="run-usage" title="模型用量" hint="提供商返回的实际用量，按主 Agent 与后台模型分开显示。"><UsagePanel data={current}/></Section>
    <Section id="run-budget" title="运行预算" hint="Token 与模型调用次数均可触发提醒、收尾或上限。"><BudgetPanel data={current}/></Section>
    <Section id="run-context" title="上下文" hint="逐步检查模型输入构成与历史压缩。"><ContextPanel events={current.events}/></Section>
    <Section id="run-activity" title="活动时间线" hint="按发生顺序概括模型、工具、审批和运行状态。"><ActivityPanel events={current.events}/></Section>
    <Section id="run-artifacts" title="本次交付物" hint="只显示当前 Run 发布的文件和链接。"><ArtifactPanel runId={selected}/></Section>
    <Section id="run-trace" title="原始技术 Trace" hint="按模型步骤分组；展开事件可查看完整原始字段。"><TracePanel events={current.events}/></Section>
    <footer className="run-technical-footer"><span>Run ID</span><code>{current.trace.run_id}</code><span>事件</span><strong>{current.trace.event_count}</strong><span>步骤</span><strong>{current.trace.steps}</strong></footer>
  </>}</div></div></div></section>
}
