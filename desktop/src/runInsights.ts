import type { ContextSnapshot, TraceEvent } from './types'

export interface ContextStep {
  step:number
  context:ContextSnapshot
  eventTime:string
}

export interface ActivityEntry {
  id:string
  time:string
  step:number
  title:string
  description:string
  tone:'normal'|'success'|'warning'|'danger'
}

export function traceStep(event:TraceEvent):number {
  return typeof event.step==='number'&&event.step>0?event.step:0
}

export function contextSteps(events:TraceEvent[]):ContextStep[] {
  return events.filter(event=>event.type==='context_prepared'&&event.context)
    .map(event=>({step:traceStep(event),context:event.context!,eventTime:event.event_time}))
    .sort((left,right)=>left.step-right.step)
}

export function eventCategory(event:TraceEvent):'model'|'tools'|'approval'|'context'|'memory' {
  if(event.type.includes('approval'))return 'approval'
  if(event.type.includes('tool'))return 'tools'
  if(event.type.includes('context')||event.type.includes('budget'))return 'context'
  if(event.type.includes('memory')||event.type.includes('post_run')||event.type==='auxiliary_model_completed')return 'memory'
  return 'model'
}

export function eventTitle(event:TraceEvent):string {
  const labels:Record<string,string>={
    agent_started:'运行开始',agent_completed:'运行完成',agent_failed:'运行失败',run_recovered:'从检查点恢复',
    context_prepared:'上下文已准备',assistant_message:'模型回复',
    tool_call:'调用工具',tool_result:'工具返回',
    approval_requested:'等待审批',approval_resolved:'审批已处理',
    run_budget_warning:'接近运行预算',run_budget_finalizing:'准备结束运行',run_budget_exceeded:'运行预算超限',
    auxiliary_model_completed:'辅助模型完成',post_run_started:'运行后处理开始',
    post_run_completed:'运行后处理完成',post_run_failed:'运行后处理失败',
  }
  return labels[event.type]||event.type.replaceAll('_',' ')
}

export function activityEntries(events:TraceEvent[]):ActivityEntry[] {
  const toolNames=new Map<string,string>()
  for(const event of events){
    if(event.tool_call?.id)toolNames.set(event.tool_call.id,event.tool_call.name)
  }
  const entries:ActivityEntry[]=[]
  for(const event of events){
    let description=''
    let tone:ActivityEntry['tone']='normal'
    let title=eventTitle(event)
    switch(event.type){
      case 'agent_started':description='Agent 已接收任务。';break
      case 'run_recovered':description=`沿用之前 ${event.inherited_model_calls||0} 次模型调用与 ${formatTokens((event.inherited_usage as {total_tokens?:number}|undefined)?.total_tokens)} tokens 的运行预算。`;break
      case 'context_prepared':
        if(!event.context?.compacted)continue
        description=`输入从 ${formatTokens(event.context.original_estimated_tokens)} 压缩到 ${formatTokens(event.context.prepared_input_tokens)} tokens；移除 ${event.context.dropped_messages} 条较早消息。`
        tone='warning';break
      case 'assistant_message':
        description=event.message?.tool_calls?.length
          ?`模型选择了 ${event.message.tool_calls.length} 个工具；本步使用 ${formatTokens(event.usage?.total_tokens)} tokens。`
          :`模型给出回复；本步使用 ${formatTokens(event.usage?.total_tokens)} tokens。`
        break
      case 'tool_call':title=`调用 ${event.tool_call?.name||'工具'}`;description='正在执行工具操作。';break
      case 'tool_result':{
        const result=event.tool_result
        title=`${result?.success?'完成':'失败'} · ${result?.tool_name||toolNames.get(result?.tool_call_id||'')||'工具'}`
        description=`耗时 ${formatDuration(result?.duration_ms)}${result?.success?'':`；${result?.error||'执行失败'}`}`
        tone=result?.success?'success':'danger';break
      }
      case 'approval_requested':description=`${String(event.tool_name||'工具')} 需要用户允许。`;tone='warning';break
      case 'approval_resolved':description=`${String(event.tool_name||'工具')}：${String(event.approval_status||'已处理')}。`;tone=event.approval_status==='approved'?'success':'warning';break
      case 'run_budget_warning':case 'run_budget_finalizing':case 'run_budget_exceeded':
        description=`原因：${event.run_budget_reason==='model_calls'?'模型调用次数':'Token 用量'}；已用 ${formatTokens(event.run_budget_chargeable_tokens as number)} tokens、${event.run_budget_model_calls} 次调用。`
        tone=event.type==='run_budget_exceeded'?'danger':'warning';break
      case 'auxiliary_model_completed':
        description=`${roleLabel(event.role)}使用 ${formatTokens(event.usage?.total_tokens)} tokens。`;break
      case 'agent_completed':description='任务已完成。';tone='success';break
      case 'agent_failed':description=typeof event.error==='object'?event.error?.message||'运行失败。':String(event.error||'运行失败。');tone='danger';break
      case 'post_run_started':description='正在整理记忆和可复用经验。';break
      case 'post_run_completed':description='运行后处理已完成。';tone='success';break
      case 'post_run_failed':description=typeof event.error==='object'?event.error?.message||'运行后处理失败。':String(event.error||'运行后处理失败。');tone='danger';break
      default:continue
    }
    entries.push({id:event.event_id,time:event.event_time,step:traceStep(event),title,description,tone})
  }
  return entries
}

export function roleLabel(role?:string):string {
  return ({context_summary:'上下文摘要',memory_reflection:'记忆反思',skill_learning:'技能学习'} as Record<string,string>)[role||'']||'辅助模型'
}

export function formatTokens(value:number|null|undefined):string {
  return value===null||value===undefined?'未提供':new Intl.NumberFormat('zh-CN').format(value)
}

export function formatDuration(milliseconds:number|null|undefined):string {
  if(milliseconds===null||milliseconds===undefined)return '未知'
  return milliseconds<1000?`${milliseconds} ms`:`${(milliseconds/1000).toFixed(1)} s`
}
