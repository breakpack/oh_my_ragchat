import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  api,
  streamSse,
  fmtSize,
  type Attachment,
  type AttachmentIn,
  type Citation,
  type Message,
  type Persona,
  type Session,
} from '../api'
import { Modal, Toggle, useRun, useToast } from '../ui'

const IMAGE_RE = /\.(png|jpe?g|webp|bmp|tiff?|gif)$/i

export interface ModelOpt {
  name: string
  family?: string | null
  embedding?: boolean
  remote?: boolean
  label?: string
}

// 원격 제공자별 optgroup 이름. 여기 없는 family 는 이름 그대로 쓴다.
const PROVIDER_LABEL: Record<string, string> = {
  deepseek: 'DeepSeek API',
  openai: 'OpenAI API',
  anthropic: 'Claude API',
}

/** 로컬(Ollama) / 원격을 제공자별 optgroup 으로 나눠 보여준다. */
function ModelOptions({ models }: { models: ModelOpt[] }) {
  const local = models.filter((m) => !m.remote)
  const remote = models.filter((m) => m.remote)
  const families = [...new Set(remote.map((m) => m.family || 'remote'))]

  return (
    <>
      {!!local.length && (
        <optgroup label="로컬 (Ollama)">
          {local.map((m) => (
            <option key={m.name} value={m.name}>{m.name}</option>
          ))}
        </optgroup>
      )}
      {families.map((f) => (
        <optgroup key={f} label={PROVIDER_LABEL[f] ?? f}>
          {remote
            .filter((m) => (m.family || 'remote') === f)
            .map((m) => (
              <option key={m.name} value={m.name}>{m.label || m.name}</option>
            ))}
        </optgroup>
      ))}
    </>
  )
}

/** 첨부 대기 항목. data 는 base64(접두어 제거), preview 는 화면 표시용 data URL. */
interface Pending {
  name: string
  size: number
  data: string
  preview?: string
}

function readAsBase64(file: File): Promise<Pending> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error(`${file.name} 을(를) 읽지 못했습니다`))
    reader.onload = () => {
      const url = String(reader.result)
      const comma = url.indexOf(',')
      resolve({
        name: file.name,
        size: file.size,
        data: url.slice(comma + 1),
        preview: IMAGE_RE.test(file.name) ? url : undefined,
      })
    }
    reader.readAsDataURL(file)
  })
}

const MODES = [
  { v: 'hybrid', label: '하이브리드' },
  { v: 'naive', label: '벡터만' },
  { v: 'local', label: '엔티티 중심' },
  { v: 'global', label: '관계 중심' },
]

export default function Chat() {
  const { sessionId } = useParams()
  const navigate = useNavigate()
  const run = useRun()
  const toast = useToast()

  const [sessions, setSessions] = useState<Session[]>([])
  const [personas, setPersonas] = useState<Persona[]>([])
  const [models, setModels] = useState<ModelOpt[]>([])
  const [session, setSession] = useState<Session | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [newOpen, setNewOpen] = useState(false)
  const [cite, setCite] = useState<Citation | null>(null)
  const [pending, setPending] = useState<Pending[]>([])
  const [dragging, setDragging] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const attachRef = useRef<HTMLInputElement>(null)

  const id = sessionId ? Number(sessionId) : null

  const loadSessions = useCallback(async () => {
    const r = await run(() => api.get<{ sessions: Session[] }>('/api/sessions'))
    if (r) setSessions(r.sessions)
    return r?.sessions ?? []
  }, [run])

  useEffect(() => {
    ;(async () => {
      const list = await loadSessions()
      const p = await run(() => api.get<{ personas: Persona[] }>('/api/personas'))
      if (p) setPersonas(p.personas)
      try {
        const m = await api.get<{ models: ModelOpt[] }>('/api/settings/models')
        setModels(m.models.filter((x) => !x.embedding))
      } catch {
        /* Ollama 가 꺼져 있어도 채팅 화면은 열려야 한다 */
      }
      if (!sessionId && list.length) navigate(`/chat/${list[0].id}`, { replace: true })
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!id) {
      setSession(null)
      setMessages([])
      return
    }
    ;(async () => {
      const s = await run(() => api.get<{ session: Session }>(`/api/sessions/${id}`))
      if (s) setSession(s.session)
      const m = await run(() => api.get<{ messages: Message[] }>(`/api/sessions/${id}/messages`))
      if (m) setMessages(m.messages)
    })()
  }, [id, run])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: busy ? 'auto' : 'smooth' })
  }, [messages, busy])

  const patchSession = async (patch: Partial<Session>) => {
    if (!id) return
    const r = await run(() => api.patch<{ session: Session }>(`/api/sessions/${id}`, patch))
    if (r) {
      setSession(r.session)
      setSessions((prev) => prev.map((s) => (s.id === r.session.id ? { ...s, ...r.session } : s)))
    }
  }

  const createSession = async (body: Partial<Session>) => {
    const r = await run(() => api.post<{ session: Session }>('/api/sessions', body))
    if (r) {
      setSessions((prev) => [r.session, ...prev])
      navigate(`/chat/${r.session.id}`)
      setNewOpen(false)
    }
  }

  const removeSession = async (sid: number) => {
    if (!confirm('이 대화를 삭제할까요?')) return
    await run(() => api.del(`/api/sessions/${sid}`))
    const rest = sessions.filter((s) => s.id !== sid)
    setSessions(rest)
    if (id === sid) navigate(rest.length ? `/chat/${rest[0].id}` : '/chat', { replace: true })
  }

  const addFiles = async (files: FileList | File[]) => {
    const items = await Promise.all(Array.from(files).map(readAsBase64)).catch((e) => {
      toast(e.message, true)
      return [] as Pending[]
    })
    if (items.length) setPending((prev) => [...prev, ...items])
  }

  const send = async () => {
    const text = input.trim()
    if (!text || !id || busy) return

    const attachments: AttachmentIn[] = pending.map((p) => ({ name: p.name, data: p.data }))
    const shown: Attachment[] = pending.map((p) => ({
      kind: p.preview ? 'image' : 'text',
      name: p.name,
    }))

    setInput('')
    setPending([])
    setMessages((prev) => [
      ...prev,
      { role: 'user', content: text, attachments: shown },
      { role: 'assistant', content: '', pending: true, citations: [] },
    ])
    setBusy(true)

    const ctrl = new AbortController()
    abortRef.current = ctrl
    const citations: Citation[] = []

    const patchLast = (fn: (m: Message) => Message) =>
      setMessages((prev) => prev.map((m, i) => (i === prev.length - 1 ? fn(m) : m)))

    try {
      await streamSse(
        `/api/chat/sessions/${id}/messages`,
        { content: text, attachments },
        (event, data) => {
          if (event === 'meta') {
            if (data.title) {
              setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, title: data.title } : s)))
            }
            patchLast((m) => ({ ...m, model: data.model }))
            if (data.rag && data.rag_stats && !data.rag_stats.chunks) {
              toast('RAG: 관련 자료를 찾지 못했습니다')
            }
            if (data.web && !data.web_stats?.results) {
              toast('웹·논문 검색 결과가 없습니다', true)
            }
          } else if (event === 'citation') {
            citations.push(data)
            patchLast((m) => ({ ...m, citations: [...citations] }))
          } else if (event === 'thinking') {
            patchLast((m) => ({ ...m, thinking: (m.thinking || '') + data.t }))
          } else if (event === 'token') {
            patchLast((m) => ({ ...m, content: m.content + data.t }))
          } else if (event === 'done') {
            patchLast((m) => ({ ...m, pending: false }))
          } else if (event === 'saved') {
            patchLast((m) => ({ ...m, id: data.message_id, pending: false }))
          } else if (event === 'error') {
            toast(data.message || '응답 생성에 실패했습니다', true)
            patchLast((m) => ({ ...m, pending: false }))
          }
        },
        ctrl.signal,
      )
    } catch (e: any) {
      if (e?.name !== 'AbortError') toast(e?.message || String(e), true)
    } finally {
      patchLast((m) => ({ ...m, pending: false }))
      setBusy(false)
      abortRef.current = null
      taRef.current?.focus()
    }
  }

  const stop = async () => {
    if (id) await api.post(`/api/chat/stop/${id}`).catch(() => {})
    abortRef.current?.abort()
  }

  return (
    <div className="chat">
      <aside className="sessions">
        <header>
          <button className="primary" style={{ width: '100%' }} onClick={() => setNewOpen(true)}>
            + 새 대화
          </button>
        </header>
        <div className="list">
          {sessions.map((s) => (
            <div
              key={s.id}
              className={`item${s.id === id ? ' active' : ''}`}
              role="button"
              tabIndex={0}
              aria-current={s.id === id}
              onClick={() => navigate(`/chat/${s.id}`)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  navigate(`/chat/${s.id}`)
                }
              }}
            >
              <div className="grow truncate">
                <div className="t truncate">{s.title}</div>
                <div className="mute2 truncate">
                  {s.persona_name || '기본'}
                  {s.rag_enabled && ' · RAG'}
                  {s.web_enabled && ' · 웹'}
                </div>
              </div>
              <button
                className="ghost sm x"
                aria-label={`'${s.title}' 대화 삭제`}
                onClick={(e) => {
                  e.stopPropagation()
                  removeSession(s.id)
                }}
              >
                ✕
              </button>
            </div>
          ))}
          {!sessions.length && <div className="empty">대화가 없습니다</div>}
        </div>
      </aside>

      <div className="thread">
        {session ? (
          <>
            <div className="bar">
              <span className="title truncate" style={{ maxWidth: 240 }}>{session.title}</span>

              <Toggle checked={session.rag_enabled} onChange={(v) => patchSession({ rag_enabled: v })}>
                RAG
              </Toggle>

              <Toggle checked={session.web_enabled} onChange={(v) => patchSession({ web_enabled: v })}>
                웹·논문
              </Toggle>

              {session.rag_enabled && (
                <select
                  style={{ width: 130 }}
                  value={session.rag_mode}
                  onChange={(e) => patchSession({ rag_mode: e.target.value })}
                >
                  {MODES.map((m) => (
                    <option key={m.v} value={m.v}>{m.label}</option>
                  ))}
                </select>
              )}

              <select
                style={{ width: 150 }}
                value={session.persona_id ?? ''}
                onChange={(e) => patchSession({ persona_id: e.target.value ? Number(e.target.value) : null })}
              >
                <option value="">페르소나 없음</option>
                {personas.map((p) => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </select>

              <select
                style={{ width: 190 }}
                value={session.model ?? ''}
                onChange={(e) => patchSession({ model: e.target.value || null })}
              >
                <option value="">기본 모델</option>
                <ModelOptions models={models} />
              </select>
            </div>

            <div className="msgs">
              {messages.map((m, i) => (
                <MessageView key={m.id ?? `t${i}`} m={m} onCite={setCite} />
              ))}
              {!messages.length && (
                <div className="empty">
                  무엇이든 물어보세요.
                  <br />
                  <span className="mute2">RAG 를 켜면 내 문서를 근거로 답합니다.</span>
                </div>
              )}
              <div ref={bottomRef} />
            </div>

            <div
              className="composer"
              onDragOver={(e) => {
                e.preventDefault()
                setDragging(true)
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault()
                setDragging(false)
                if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files)
              }}
            >
              <div className={`box${dragging ? ' over' : ''}`}>
                {!!pending.length && (
                  <div className="attachments">
                    {pending.map((p, i) => (
                      <span className="attach" key={`${p.name}-${i}`}>
                        {p.preview ? (
                          <img className="thumb" src={p.preview} alt="" />
                        ) : (
                          <span aria-hidden="true">📄</span>
                        )}
                        <span className="nm" title={p.name}>{p.name}</span>
                        <span className="mute2">{fmtSize(p.size)}</span>
                        <button
                          className="ghost sm"
                          aria-label={`${p.name} 첨부 취소`}
                          style={{ padding: '0 4px' }}
                          onClick={() => setPending((prev) => prev.filter((_, j) => j !== i))}
                        >
                          ✕
                        </button>
                      </span>
                    ))}
                  </div>
                )}

                <textarea
                  ref={taRef}
                  rows={1}
                  placeholder={
                    dragging ? '여기에 놓으면 첨부됩니다' : '무엇이든 물어보세요'
                  }
                  value={input}
                  onChange={(e) => {
                    setInput(e.target.value)
                    const el = e.target as HTMLTextAreaElement
                    el.style.height = 'auto'
                    el.style.height = `${Math.min(el.scrollHeight, 220)}px`
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                      e.preventDefault()
                      send()
                    }
                  }}
                />

                <div className="tools">
                  <button
                    className="ghost sm"
                    aria-label="파일 첨부"
                    title="이미지는 vision 모델로, 문서는 본문을 읽어 프롬프트에 넣습니다"
                    onClick={() => attachRef.current?.click()}
                  >
                    + 첨부
                  </button>
                  <input
                    ref={attachRef}
                    type="file"
                    multiple
                    hidden
                    onChange={(e) => {
                      if (e.target.files) addFiles(e.target.files)
                      e.target.value = ''
                    }}
                  />

                  <span className={`badge${session.rag_enabled ? ' on' : ''}`}>
                    {session.rag_enabled
                      ? `RAG · ${MODES.find((m) => m.v === session.rag_mode)?.label}`
                      : '일반 대화'}
                  </span>

                  {session.web_enabled && <span className="badge on">웹·논문 검색</span>}

                  <span className="hint mute2">Shift+Enter 줄바꿈</span>

                  {busy ? (
                    <button className="danger" onClick={stop}>중단</button>
                  ) : (
                    <button className="primary" onClick={send} disabled={!input.trim()}>
                      보내기
                    </button>
                  )}
                </div>
              </div>
            </div>
          </>
        ) : (
          <div className="empty">
            왼쪽에서 대화를 고르거나 <b>새 대화</b>를 시작하세요.
          </div>
        )}
      </div>

      {newOpen && (
        <NewSessionModal
          personas={personas}
          models={models}
          onClose={() => setNewOpen(false)}
          onCreate={createSession}
        />
      )}

      {cite && (
        <Modal title={`출처 ${cite.tag}`} onClose={() => setCite(null)}>
          {cite.url ? (
            <>
              <div style={{ marginBottom: 6, fontWeight: 600 }}>{cite.title}</div>
              {cite.meta && <div className="mute2" style={{ marginBottom: 6 }}>{cite.meta}</div>}
              <a href={cite.url} target="_blank" rel="noreferrer noopener" className="mono">
                {cite.url}
              </a>
              <div style={{ whiteSpace: 'pre-wrap', fontSize: 13, marginTop: 10 }}>
                {cite.excerpt}
              </div>
            </>
          ) : (
            <>
              <div className="mono mute2" style={{ marginBottom: 10 }}>{cite.path}</div>
              <div style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>{cite.excerpt}</div>
              <div className="mute2" style={{ marginTop: 10 }}>유사도 {cite.score}</div>
            </>
          )}
        </Modal>
      )}
    </div>
  )
}

/** 칩에 보일 짧은 이름. 내 문서는 파일명, 웹·논문은 제목(길면 자른다). */
function citeLabel(c: Citation) {
  if (!c.url) return c.path.split('/').pop()
  const t = c.title || c.path
  return t.length > 44 ? `${t.slice(0, 44)}…` : t
}

function MessageView({ m, onCite }: { m: Message; onCite: (c: Citation) => void }) {
  return (
    <div className={`msg ${m.role}`}>
      <div className="who" aria-label={m.role === 'user' ? '내 메시지' : '어시스턴트 메시지'}>
        {m.role === 'user' ? '나' : 'AI'}
      </div>
      <div className="body">
        {m.thinking && (
          <details className="think">
            <summary>사고 과정</summary>
            <div className="t">{m.thinking}</div>
          </details>
        )}

        {!!m.attachments?.length && (
          <div className="attachments">
            {m.attachments.map((a: Attachment, i: number) => (
              <span className="attach" key={`${a.name}-${i}`}>
                {a.kind === 'image' && a.path ? (
                  <img
                    className="thumb"
                    src={`/api/files/content?path=${encodeURIComponent(a.path)}`}
                    alt=""
                  />
                ) : (
                  <span aria-hidden="true">{a.kind === 'image' ? '🖼️' : '📄'}</span>
                )}
                <span className="nm" title={a.name}>{a.name}</span>
                {a.chars ? <span className="mute2">{a.chars}자</span> : null}
              </span>
            ))}
          </div>
        )}

        {m.role === 'user' ? (
          <div className="bubble">{m.content}</div>
        ) : (
          <div className={m.pending && !m.content ? 'cursor muted' : ''}>
            {m.content ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
            ) : (
              m.pending && '생각 중'
            )}
          </div>
        )}

        {!!m.citations?.length && (
          <div className="cites">
            {m.citations.map((c) => (
              <span
                key={c.tag}
                className="cite"
                onClick={() => onCite(c)}
                title={c.url || c.path}
              >
                [{c.tag}] {citeLabel(c)}
              </span>
            ))}
          </div>
        )}

        {m.model && !m.pending && <div className="mute2" style={{ marginTop: 6 }}>{m.model}</div>}
      </div>
    </div>
  )
}

function NewSessionModal({
  personas,
  models,
  onClose,
  onCreate,
}: {
  personas: Persona[]
  models: ModelOpt[]
  onClose: () => void
  onCreate: (body: Partial<Session>) => void
}) {
  const [title, setTitle] = useState('')
  const [personaId, setPersonaId] = useState<number | ''>(personas.find((p) => p.is_default)?.id ?? '')
  const [model, setModel] = useState('')
  const [rag, setRag] = useState(false)
  const [mode, setMode] = useState('hybrid')
  const [web, setWeb] = useState(false)

  return (
    <Modal
      title="새 대화"
      onClose={onClose}
      submitLabel="시작"
      onSubmit={() =>
        onCreate({
          title: title.trim() || undefined,
          persona_id: personaId === '' ? null : Number(personaId),
          model: model || null,
          rag_enabled: rag,
          rag_mode: mode,
          web_enabled: web,
        })
      }
    >
      <div className="field">
        <label>제목 <span className="mute2">· 비우면 첫 질문에서 자동 생성</span></label>
        <input value={title} autoFocus onChange={(e) => setTitle(e.target.value)} />
      </div>
      <div className="field">
        <label>페르소나</label>
        <select value={personaId} onChange={(e) => setPersonaId(e.target.value === '' ? '' : Number(e.target.value))}>
          <option value="">없음</option>
          {personas.map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
      </div>
      <div className="field">
        <label>모델</label>
        <select value={model} onChange={(e) => setModel(e.target.value)}>
          <option value="">기본 모델</option>
          <ModelOptions models={models} />
        </select>
      </div>
      <div className="row" style={{ marginTop: 4 }}>
        <Toggle checked={rag} onChange={setRag}>내 문서 참조 (RAG)</Toggle>
        {rag && (
          <select style={{ width: 140 }} value={mode} onChange={(e) => setMode(e.target.value)}>
            {MODES.map((m) => (
              <option key={m.v} value={m.v}>{m.label}</option>
            ))}
          </select>
        )}
      </div>
      <div className="row">
        <Toggle checked={web} onChange={setWeb}>웹·논문 검색</Toggle>
        <span className="mute2">질문할 때마다 논문 DB 와 웹을 찾아 근거로 씁니다</span>
      </div>
    </Modal>
  )
}
