import { describe, expect, it } from 'vitest'
import { approvalFacts, approvalTitle, permissionScopeLabel } from './approvalPresentation'

describe('approval presentation', () => {
  it('turns protocol data into a human-readable action', () => {
    expect(approvalTitle({tool_name:'write_file',arguments:{path:'reports/final.md'}})).toContain('reports/final.md')
    expect(approvalFacts({text:'x'.repeat(100)})[0][1]).toHaveLength(78)
    expect(permissionScopeLabel('conversation')).toBe('当前会话')
  })
})
