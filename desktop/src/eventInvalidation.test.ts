import { describe, expect, it } from 'vitest'
import { queryKeysForAgentEvent } from './eventInvalidation'

describe('event cache invalidation',()=>{
  it('does not refetch queries on streamed text',()=>{
    expect(queryKeysForAgentEvent('assistant_delta')).toEqual([])
    expect(queryKeysForAgentEvent('reasoning_delta')).toEqual([])
  })
  it('refreshes only affected data',()=>{
    expect(queryKeysForAgentEvent('tool_result')).toEqual(['trace'])
    expect(queryKeysForAgentEvent('approval_requested')).toEqual(['approvals','trace'])
    expect(queryKeysForAgentEvent('task_updated')).toEqual(['tasks','trace'])
    expect(queryKeysForAgentEvent('agent_completed')).toContain('runs')
  })
})
