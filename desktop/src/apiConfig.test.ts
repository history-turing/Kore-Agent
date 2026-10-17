import { describe, expect, it } from 'vitest'
import { httpBaseUrl } from './apiConfig'

describe('HTTP endpoint configuration', () => {
  it('derives HTTP and HTTPS origins from RPC URLs', () => {
    expect(httpBaseUrl('ws://127.0.0.1:8765/rpc')).toBe('http://127.0.0.1:8765')
    expect(httpBaseUrl('wss://agent.example.test/base/rpc')).toBe('https://agent.example.test/base')
  })
})
