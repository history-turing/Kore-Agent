import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

type Confirmation = { title: string; message: string; confirmLabel: string; destructive?: boolean }
type Pending = Confirmation & { resolve: (accepted: boolean) => void; previousFocus: HTMLElement | null }

const ConfirmContext = createContext<((confirmation: Confirmation) => Promise<boolean>) | null>(null)

export function useConfirmAction() {
  const confirm = useContext(ConfirmContext)
  if (!confirm) throw new Error('ConfirmProvider is missing')
  return confirm
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null)
  const pendingRef = useRef<Pending | null>(null)
  const cancelButton = useRef<HTMLButtonElement>(null)
  const dialog = useRef<HTMLElement>(null)

  const settle = useCallback((accepted: boolean) => {
    const current = pendingRef.current
    if (!current) return
    pendingRef.current = null
    setPending(null)
    current.resolve(accepted)
    window.setTimeout(() => current.previousFocus?.focus(), 0)
  }, [])

  const confirm = useCallback((request: Confirmation) => new Promise<boolean>(resolve => {
    if (pendingRef.current) {
      resolve(false)
      return
    }
    const next = { ...request, resolve, previousFocus: document.activeElement as HTMLElement | null }
    pendingRef.current = next
    setPending(next)
  }), [])

  useEffect(() => {
    if (!pending) return
    cancelButton.current?.focus()
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        settle(false)
      } else if (event.key === 'Tab') {
        const buttons = Array.from(dialog.current?.querySelectorAll<HTMLButtonElement>('button') || [])
        if (!buttons.length) return
        if (event.shiftKey && document.activeElement === buttons[0]) {
          event.preventDefault()
          buttons.at(-1)?.focus()
        } else if (!event.shiftKey && document.activeElement === buttons.at(-1)) {
          event.preventDefault()
          buttons[0].focus()
        }
      }
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [pending, settle])

  return <ConfirmContext.Provider value={confirm}>
    {children}
    {pending && <div className="modal-back" onMouseDown={event => { if (event.target === event.currentTarget) settle(false) }}>
      <section ref={dialog} className="modal confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-message">
        <h2 id="confirm-title">{pending.title}</h2>
        <p id="confirm-message">{pending.message}</p>
        <div className="modal-actions">
          <button ref={cancelButton} onClick={() => settle(false)}>返回</button>
          <button className={pending.destructive ? 'confirm-danger' : 'primary'} onClick={() => settle(true)}>{pending.confirmLabel}</button>
        </div>
      </section>
    </div>}
  </ConfirmContext.Provider>
}
