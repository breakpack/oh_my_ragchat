export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: {
      ...(init.body && !(init.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}),
      ...(init.headers || {}),
    },
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch { /* 본문이 JSON 이 아니면 상태줄을 그대로 쓴다 */ }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: body === undefined ? undefined : JSON.stringify(body) }),
  del: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: 'DELETE', body: body === undefined ? undefined : JSON.stringify(body) }),
}

/** 업로드 진행률이 필요할 때. fetch 는 요청 본문 진행률을 주지 않아 XHR 을 쓴다. */
export function uploadWithProgress<T>(
  path: string,
  form: FormData,
  onProgress: (loaded: number, total: number) => void,
  signal?: AbortSignal,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', path)
    xhr.withCredentials = true

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded, e.total)
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(xhr.responseText ? JSON.parse(xhr.responseText) : (undefined as T))
        } catch {
          reject(new ApiError(xhr.status, '응답을 해석할 수 없습니다'))
        }
        return
      }
      let detail = `${xhr.status} ${xhr.statusText}`
      try {
        const body = JSON.parse(xhr.responseText)
        if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
      } catch { /* 본문이 JSON 이 아니면 상태줄을 그대로 쓴다 */ }
      reject(new ApiError(xhr.status, detail))
    }

    xhr.onerror = () => reject(new ApiError(0, '네트워크 오류로 업로드하지 못했습니다'))
    xhr.onabort = () => reject(new ApiError(0, '업로드를 취소했습니다'))

    signal?.addEventListener('abort', () => xhr.abort(), { once: true })
    xhr.send(form)
  })
}

// ─────────────────────────── SSE ───────────────────────────
// EventSource 는 POST 를 못 하므로 fetch 스트림을 직접 파싱한다.

export type SseHandler = (event: string, data: any) => void

export async function streamSse(
  path: string,
  body: unknown,
  onEvent: SseHandler,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok || !res.body) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const j = await res.json()
      if (j?.detail) detail = j.detail
    } catch { /* noop */ }
    throw new ApiError(res.status, detail)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let sep: number
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const raw = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)

      let event = 'message'
      const dataLines: string[] = []
      for (const line of raw.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
      }
      if (!dataLines.length) continue
      try {
        onEvent(event, JSON.parse(dataLines.join('\n')))
      } catch {
        onEvent(event, dataLines.join('\n'))
      }
    }
  }
}

// ─────────────────────────── 타입 ───────────────────────────

export interface Session {
  id: number
  title: string
  persona_id: number | null
  persona_name?: string | null
  model: string | null
  rag_enabled: boolean
  rag_mode: string
  message_count?: number
  updated_at: string
}

export interface Persona {
  id: number
  name: string
  system_prompt: string
  model: string | null
  temperature: number | null
  is_default: boolean
}

export interface Citation {
  tag: string
  path: string
  document_id: number
  chunk_id: number
  excerpt: string
  score: number
}

export interface Attachment {
  kind: 'image' | 'text'
  name: string
  path?: string | null
  chars?: number
}

/** 전송용. path(NAS 파일) 또는 data(base64) 중 하나를 채운다. */
export interface AttachmentIn {
  name: string
  path?: string
  data?: string
}

export interface Message {
  id?: number
  role: 'user' | 'assistant' | 'system'
  content: string
  thinking?: string | null
  citations?: Citation[] | null
  attachments?: Attachment[] | null
  model?: string | null
  created_at?: string
  pending?: boolean
}

export interface Preview {
  path: string
  name: string
  ext: string
  size: number
  mime: string
  kind: 'image' | 'pdf' | 'text' | 'markdown' | 'none'
  text?: string
  truncated?: boolean
  error?: string
}

export interface FileItem {
  name: string
  path: string
  is_dir: boolean
  size: number | null
  mtime: number
  ext: string
  hidden: boolean
  locked: boolean
  note: string | null
  indexable: boolean
}

export interface DocRow {
  id: number
  path: string
  size: number | null
  status: string
  chunk_count: number
  error: string | null
  indexed_at: string | null
  entity_count: number
  ocr: boolean
  progress_done: number
  progress_total: number
  phase: string | null
}

/** /api/rag/events 로 흘러오는 워커 이벤트. */
export interface RagEvent {
  kind: 'document' | 'progress' | 'scan'
  document_id?: number
  path?: string
  status?: string
  done?: number
  total?: number
  phase?: string
  error?: string | null
  chunk_count?: number | null
  queued?: number
  removed?: number
}

export function fmtSize(n: number | null | undefined): string {
  if (n === null || n === undefined) return '-'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

export function fmtTime(t: number | string | null | undefined): string {
  if (!t) return '-'
  const d = typeof t === 'number' ? new Date(t * 1000) : new Date(t)
  if (Number.isNaN(d.getTime())) return '-'
  return d.toLocaleString('ko-KR', { dateStyle: 'short', timeStyle: 'short' })
}
