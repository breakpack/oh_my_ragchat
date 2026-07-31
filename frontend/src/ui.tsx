import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'

// ─────────────────────────── 토스트 ───────────────────────────

interface Toast { id: number; text: string; error?: boolean }
const ToastCtx = createContext<(text: string, error?: boolean) => void>(() => {})

export function useToast() {
  return useContext(ToastCtx)
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([])

  const push = useCallback((text: string, error = false) => {
    const id = Date.now() + Math.random()
    setItems((prev) => [...prev, { id, text, error }])
    setTimeout(() => setItems((prev) => prev.filter((t) => t.id !== id)), error ? 6000 : 3000)
  }, [])

  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div>
        {items.map((t, i) => (
          <div key={t.id} className={`toast${t.error ? ' err' : ''}`} style={{ bottom: 18 + i * 58 }}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}

/** 에러를 토스트로 흘리는 래퍼. */
export function useRun() {
  const toast = useToast()
  return useCallback(
    async <T,>(fn: () => Promise<T>, okMessage?: string): Promise<T | undefined> => {
      try {
        const result = await fn()
        if (okMessage) toast(okMessage)
        return result
      } catch (e: any) {
        toast(e?.message || String(e), true)
        return undefined
      }
    },
    [toast],
  )
}

// ─────────────────────────── 모달 ───────────────────────────

export function Modal({
  title,
  children,
  onClose,
  onSubmit,
  submitLabel = '확인',
  danger,
  busy,
}: {
  title: string
  children: ReactNode
  onClose: () => void
  onSubmit?: () => void
  submitLabel?: string
  danger?: boolean
  busy?: boolean
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <form
        className="modal"
        onSubmit={(e) => {
          e.preventDefault()
          onSubmit?.()
        }}
      >
        <h3>{title}</h3>
        {children}
        <footer>
          <button type="button" onClick={onClose}>취소</button>
          {onSubmit && (
            <button type="submit" className={danger ? 'danger' : 'primary'} disabled={busy}>
              {busy ? '처리 중…' : submitLabel}
            </button>
          )}
        </footer>
      </form>
    </div>
  )
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="field">
      <label>
        {label} {hint && <span className="mute2">· {hint}</span>}
      </label>
      {children}
    </div>
  )
}

export function Toggle({
  checked,
  onChange,
  children,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  children: ReactNode
}) {
  return (
    <label className={`toggle${checked ? ' on' : ''}`}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className={`dot${checked ? ' on' : ''}`} />
      {children}
    </label>
  )
}
