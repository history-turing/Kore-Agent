import { describe, expect, it } from 'vitest'
import { activityEntries, contextSteps, eventCategory } from './runInsights'
import type { TraceEvent } from './types'

function event(type:string, step:number, extra:Partial<TraceEvent>={}):TraceEvent {
  return {event_id:`${type}-${step}`,run_id:'run',type,step,sequence:step,event_time:'2026-10-02T08:00:00Z',...extra}
}

describe('run detail presentation',()=>{
  it('keeps context snapshots separate for each model step',()=>{
    const snapshots=contextSteps([
      event('context_prepared',2,{context:{estimated_tokens:500,original_estimated_tokens:500,prepared_input_tokens:500,context_window:4000,max_input_tokens:3000,working_input_budget:2500,tool_result_budget_tokens:1000,tool_schema_tokens_estimated:50,tool_result_tokens_before:20,tool_result_tokens_after:20,breakdown:{system:100,messages:330,tool_results:20,tool_schemas:50},compacted:false,compaction_stage:'none',summary_validated:false,summary_updated:false,dropped_messages:0}}),
      event('context_prepared',1,{context:{estimated_tokens:300,original_estimated_tokens:600,prepared_input_tokens:300,context_window:4000,max_input_tokens:3000,working_input_budget:2500,tool_result_budget_tokens:1000,tool_schema_tokens_estimated:50,tool_result_tokens_before:200,tool_result_tokens_after:40,breakdown:{system:100,messages:110,tool_results:40,tool_schemas:50},compacted:true,compaction_stage:'summary_then_history',summary_validated:true,summary_updated:true,dropped_messages:3}}),
    ])
    expect(snapshots.map(item=>item.step)).toEqual([1,2])
    expect(snapshots[0].context.original_estimated_tokens).toBe(600)
    expect(snapshots[1].context.breakdown.tool_results).toBe(20)
  })

  it('turns tool outcomes and budget events into readable activity',()=>{
    const entries=activityEntries([
      event('tool_call',1,{tool_call:{id:'call-1',name:'web_search'}}),
      event('tool_result',1,{tool_result:{tool_call_id:'call-1',success:false,error:'timeout',duration_ms:1500}}),
      event('run_budget_finalizing',2,{run_budget_reason:'model_calls',run_budget_chargeable_tokens:500,run_budget_model_calls:20}),
    ])
    expect(entries.map(item=>item.title)).toEqual(['调用 web_search','失败 · web_search','准备结束运行'])
    expect(entries[1].description).toContain('timeout')
    expect(entries[2].description).toContain('模型调用次数')
    expect(eventCategory(event('auxiliary_model_completed',2))).toBe('memory')
  })
})
