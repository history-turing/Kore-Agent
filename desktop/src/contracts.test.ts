import { describe, expect, it } from 'vitest'
import { RpcError } from './rpc'

describe('RPC protocol', () => {
  it('preserves structured errors', () => {
    const error = new RpcError(-32000, 'missing', { id: 'x' })
    expect(error.code).toBe(-32000)
    expect(error.message).toBe('missing')
    expect(error.data).toEqual({ id: 'x' })
  })
})
