export interface Conversation { id: string; title: string; created_at: string; updated_at: string; message_count: number }
export interface Message { role: 'system'|'user'|'assistant'|'tool'; content: string|null; name?: string|null; reasoning?: string|null; tool_call_id?:string|null; tool_calls?:Array<{id:string;name:string;arguments:Record<string,unknown>}> }
export interface Run { id:string; conversation_id:string|null; status:string; user_message:string; created_at:string; updated_at:string; error:string|null; stop_reason?:string|null; mode:'normal'|'plan'; source?:string|null; recovered_from_run_id?:string|null }
export interface Approval { id:string; run_id:string|null; conversation_id?:string|null; tool_name:string; arguments:Record<string,unknown>; reason:string; status:string; created_at:string; resolved_at?:string|null; resolution_scope?:PermissionScope; ui_scope?:string; available_scopes?:PermissionScope[] }
export type PermissionScope='once'|'run'|'conversation'|'global'
export interface Automation { id:string; title:string; prompt:string; status:string; schedule:{kind:string;run_at:string|null;interval_seconds:number|null;cron_expr:string|null;timezone:string}; next_run_at:string|null; last_run_at:string|null; last_run_id?:string|null }
export interface Artifact { id:string;kind:'file'|'url';title:string;description:string|null;filename:string|null;mime_type:string|null;size_bytes:number;created_at:string;source_url?:string|null;run_id?:string|null;conversation_id?:string|null }
export interface Task { id:string;title:string;description:string|null;goal?:string|null;status:string;steps:Array<{id:string;title:string;description?:string;status:string}>;updated_at:string }
export interface ModelUsage {
  input_tokens:number; output_tokens:number; total_tokens:number; model_calls:number;
  cached_input_tokens:number|null; uncached_input_tokens:number|null;
  cache_read_input_tokens:number|null; cache_write_input_tokens:number|null;
}
export interface RunBudgetSummary {
  run_budget_status:string; run_budget_reason:string|null;
  run_budget_chargeable_tokens:number; run_budget_model_calls:number;
  run_budget_warning_tokens:number; run_budget_finalization_tokens:number; run_budget_hard_tokens:number;
  run_budget_warning_model_calls:number; run_budget_finalization_model_calls:number; run_budget_hard_model_calls:number;
}
export interface RunUsageSummary {
  main_agent:ModelUsage; context_summary:ModelUsage; memory_reflection:ModelUsage;
  memory_maintenance?:ModelUsage; skill_learning:ModelUsage; provider_total:ModelUsage; budget:RunBudgetSummary;
}
export interface ContextSnapshot {
  estimated_tokens:number; original_estimated_tokens:number; prepared_input_tokens:number;
  context_window:number|null; max_input_tokens:number|null; working_input_budget:number|null;
  tool_result_budget_tokens:number|null; tool_schema_tokens_estimated:number;
  tool_result_tokens_before:number; tool_result_tokens_after:number;
  breakdown:{system:number;messages:number;tool_results:number;tool_schemas:number};
  compacted:boolean; compaction_stage:string; summary_validated:boolean;
  summary_updated:boolean; dropped_messages:number;
}
export interface TraceEvent {
  event_id:string; run_id:string; type:string; event_time:string; sequence:number;
  step?:number|null; provider?:string; model?:string; role?:string;
  usage?:Partial<ModelUsage>|null; context?:ContextSnapshot;
  tool_call?:{id?:string;name:string;arguments?:Record<string,unknown>}|null;
  tool_result?:{tool_call_id?:string;tool_name?:string;success?:boolean;output?:string;error?:string;duration_ms?:number}|null;
  message?:{role?:string;content?:string|null;tool_calls?:unknown[]}|null;
  error?:{type?:string;message?:string}|string|null;
  [key:string]:unknown;
}
export interface RunTracePayload {
  trace:{run_id:string;conversation_id:string|null;status:string;started_at:string;completed_at:string|null;
    provider:string;model:string;steps:number;stop_reason:string|null;input_tokens:number;output_tokens:number;
    total_tokens:number;event_count:number};
  events:TraceEvent[];usage_summary:RunUsageSummary;
}
export interface MemoryItem { id:string; title:string; summary:string; content?:string; status:string; access_count:number; revision:number; created_at:string; updated_at:string }
