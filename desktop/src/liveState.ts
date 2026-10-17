import type { Approval } from './types'

export type LiveTool={id:string;name:string;status:'running'|'completed'|'failed';summary?:string}
export type LiveRunState={runId:string;content:string;reasoning:string;tools:string[];toolActivity:LiveTool[];status:string;budgetStatus?:string}

export function reduceAgentEvent(previous:LiveRunState|null,event:any):LiveRunState{
  // Start a clean live buffer when the event belongs to a different run.
  const current:LiveRunState=previous&&previous.runId===event.run_id
    ? previous
    : {runId:String(event.run_id||''),content:'',reasoning:'',tools:[],toolActivity:[],status:'running'}
  if(event.type==='assistant_delta')return{...current,content:current.content+String(event.delta||'')}
  if(event.type==='reasoning_delta')return{...current,reasoning:current.reasoning+String(event.delta||'')}
  if(event.type==='tool_call'){
    const name=String(event.tool_call?.name||'tool')
    const id=String(event.tool_call?.id||`${name}:${current.toolActivity.length}`)
    return{...current,tools:[...current.tools,name],toolActivity:[...current.toolActivity,{id,name,status:'running'}]}
  }
  if(event.type==='tool_result'){
    const id=String(event.tool_result?.tool_call_id||event.tool_call_id||'')
    const success=event.tool_result?.success!==false
    // Match by call ID: tools with the same name can be active in one run.
    return{...current,toolActivity:current.toolActivity.map(item=>item.id===id?{...item,status:success?'completed':'failed',summary:String(event.tool_result?.error||event.tool_result?.output||'').slice(0,160)}:item)}
  }
  if(event.type?.startsWith('run_budget_'))return{...current,budgetStatus:event.type.replace('run_budget_','')}
  if(event.type==='agent_failed')return{...current,status:'failed'}
  if(event.type==='agent_completed')return{...current,status:'completed'}
  return current
}

export function queueDesktopApproval(queue:Approval[],approval:Approval):Approval[]{
  // Computer approvals are also surfaced in the dedicated desktop window.
  const isDesktop=approval.ui_scope==='desktop'||approval.tool_name.startsWith('computer_')
  if(!isDesktop||queue.some(item=>item.id===approval.id))return queue
  return[...queue,approval]
}

export function resolveApproval(queue:Approval[],approvalId:string):Approval[]{
  return queue.filter(item=>item.id!==approvalId)
}
