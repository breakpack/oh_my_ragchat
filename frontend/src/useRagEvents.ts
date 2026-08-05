import { useEffect, useRef, useState } from 'react'
import type { RagEvent } from './api'

/** 워커 진행률 SSE 구독. 끊기면 지수 백오프로 다시 붙고, live 상태를 알려준다. */
export function useRagEvents(onEvent: (ev: RagEvent) => void) {
  const [live, setLive] = useState(false)
  const cb = useRef(onEvent)
  cb.current = onEvent

  useEffect(() => {
    let es: EventSource | null = null
    let retry: ReturnType<typeof setTimeout> | null = null
    let attempt = 0
    let disposed = false

    const connect = () => {
      if (disposed) return
      es = new EventSource('/api/rag/events')
      es.onopen = () => {
        attempt = 0
        setLive(true)
      }
      es.onmessage = (e) => {
        try {
          cb.current(JSON.parse(e.data))
        } catch {
          /* 하트비트 등은 무시 */
        }
      }
      es.onerror = () => {
        setLive(false)
        // 502 같은 치명적 응답이면 브라우저가 재연결을 포기하고 CLOSED 로 남긴다
        if (es && es.readyState === EventSource.CLOSED) {
          es.close()
          es = null
          retry = setTimeout(connect, Math.min(2000 * 2 ** attempt++, 30000))
        }
      }
    }
    connect()

    return () => {
      disposed = true
      es?.close()
      if (retry) clearTimeout(retry)
    }
  }, [])

  return live
}
