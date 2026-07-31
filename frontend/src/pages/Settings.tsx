import { useCallback, useEffect, useState } from 'react'
import { api, fmtSize, type Persona } from '../api'
import { Field, Modal, Toggle, useRun, useToast } from '../ui'

type Tab = 'conn' | 'models' | 'rag' | 'nas' | 'security' | 'personas'

interface ModelInfo {
  name: string
  size: number
  embedding: boolean
  thinking: boolean
  parameter_size: string | null
}

export default function Settings() {
  const run = useRun()
  const toast = useToast()
  const [tab, setTab] = useState<Tab>('conn')
  const [cfg, setCfg] = useState<Record<string, any> | null>(null)
  const [dirty, setDirty] = useState<Record<string, any>>({})
  const [models, setModels] = useState<ModelInfo[]>([])
  const [health, setHealth] = useState<any>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    const r = await run(() => api.get<{ settings: Record<string, any> }>('/api/settings'))
    if (r) setCfg(r.settings)
  }, [run])

  const loadModels = useCallback(async () => {
    try {
      const m = await api.get<{ models: ModelInfo[] }>('/api/settings/models')
      setModels(m.models)
    } catch {
      setModels([])
    }
  }, [])

  const loadHealth = useCallback(async () => {
    try {
      setHealth(await api.get<any>('/api/health'))
    } catch (e: any) {
      setHealth({ ok: false, error: e?.message })
    }
  }, [])

  useEffect(() => {
    load()
    loadModels()
    loadHealth()
  }, [load, loadModels, loadHealth])

  const val = (k: string) => (k in dirty ? dirty[k] : cfg?.[k])
  const set = (k: string, v: any) => setDirty((d) => ({ ...d, [k]: v }))

  const save = async () => {
    if (!Object.keys(dirty).length) return
    setSaving(true)
    const r = await run(() => api.put<{ settings: Record<string, any> }>('/api/settings', dirty), '저장했습니다')
    setSaving(false)
    if (r) {
      setCfg(r.settings)
      setDirty({})
      loadModels()
      loadHealth()
    }
  }

  if (!cfg) return <div className="page"><div className="muted">불러오는 중…</div></div>

  const chatModels = models.filter((m) => !m.embedding)
  const embedModels = models.filter((m) => m.embedding)
  const hasChanges = Object.keys(dirty).length > 0

  return (
    <div className="page">
      <header className="row">
        <div className="grow">
          <h2>설정</h2>
          <div className="mute2">모든 값은 DB 에 저장되며 즉시 적용됩니다.</div>
        </div>
        {hasChanges && (
          <>
            <button onClick={() => setDirty({})}>되돌리기</button>
            <button className="primary" onClick={save} disabled={saving}>
              {saving ? '저장 중…' : `저장 (${Object.keys(dirty).length})`}
            </button>
          </>
        )}
      </header>

      <div className="tabs">
        {([
          ['conn', '연결'],
          ['models', '모델'],
          ['rag', 'RAG'],
          ['nas', 'NAS'],
          ['security', '보안'],
          ['personas', '페르소나'],
        ] as [Tab, string][]).map(([t, label]) => (
          <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>{label}</button>
        ))}
      </div>

      {tab === 'conn' && (
        <>
          <div className="card">
            <h3>상태</h3>
            <div className="row wrap" style={{ gap: 14 }}>
              <span className="row" style={{ gap: 6 }}>
                <span className={`dot ${health?.db?.ok ? 'ok' : 'err'}`} /> DB
              </span>
              <span className="row" style={{ gap: 6 }}>
                <span className={`dot ${health?.ollama?.ok ? 'ok' : 'err'}`} /> Ollama
                {health?.ollama?.ok && <span className="mute2">모델 {health.ollama.models}개</span>}
              </span>
              <button className="sm right" onClick={loadHealth}>새로고침</button>
            </div>
            {health?.ollama?.error && (
              <div className="mute2" style={{ color: 'var(--err)', marginTop: 8 }}>{health.ollama.error}</div>
            )}
            {health?.stats && (
              <div className="mute2" style={{ marginTop: 10 }}>
                문서 {health.stats.ready}/{health.stats.documents} · 청크 {health.stats.chunks} ·
                엔티티 {health.stats.entities} · 관계 {health.stats.relations} ·
                큐 {health.stats.queued} (실패 {health.stats.failed})
              </div>
            )}
          </div>

          <div className="card">
            <h3>Ollama</h3>
            <Field label="Base URL" hint="호스트에서 네이티브로 실행 중인 Ollama">
              <input value={val('ollama_base_url') ?? ''} onChange={(e) => set('ollama_base_url', e.target.value)} />
            </Field>
            <div className="mute2">
              macOS 컨테이너는 Metal GPU 를 못 쓰므로 Ollama 는 호스트에서 돌리고
              <code> host.docker.internal:11434 </code>로 접속합니다.
            </div>
          </div>

          <div className="card flush">
            <table>
              <thead>
                <tr><th>설치된 모델</th><th style={{ width: 110 }}>파라미터</th><th style={{ width: 100 }}>크기</th><th style={{ width: 130 }}>기능</th></tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <tr key={m.name}>
                    <td className="mono">{m.name}</td>
                    <td className="mute2">{m.parameter_size || '-'}</td>
                    <td className="mute2">{fmtSize(m.size)}</td>
                    <td>
                      {m.embedding && <span className="badge">임베딩</span>}
                      {m.thinking && <span className="badge">thinking</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!models.length && <div className="empty">모델 목록을 가져오지 못했습니다</div>}
          </div>
        </>
      )}

      {tab === 'models' && (
        <div className="card">
          <h3>모델 <span className="mute2">채팅 · 그래프 추출 · 임베딩</span></h3>
          <div className="grid2">
            <Field label="채팅 모델">
              <ModelSelect list={chatModels} value={val('chat_model')} onChange={(v) => set('chat_model', v)} />
            </Field>
            <Field label="그래프 추출 모델" hint="빠른 모델 권장">
              <ModelSelect list={chatModels} value={val('extract_model')} onChange={(v) => set('extract_model', v)} />
            </Field>
            <Field label="임베딩 모델" hint="1024차원 필요 (bge-m3)">
              <ModelSelect list={embedModels} value={val('embed_model')} onChange={(v) => set('embed_model', v)} />
            </Field>
          </div>

          <div className="grid2" style={{ marginTop: 8 }}>
            <Field label={`temperature · ${val('temperature')}`}>
              <input
                type="range" min={0} max={2} step={0.05}
                value={val('temperature')}
                onChange={(e) => set('temperature', Number(e.target.value))}
              />
            </Field>
            <Field label="num_ctx" hint="컨텍스트 토큰">
              <input type="number" value={val('num_ctx')} onChange={(e) => set('num_ctx', Number(e.target.value))} />
            </Field>
            <Field label="히스토리 턴 수" hint="모델에 보내는 최근 대화">
              <input type="number" value={val('history_turns')} onChange={(e) => set('history_turns', Number(e.target.value))} />
            </Field>
          </div>

          <Toggle checked={!!val('show_thinking')} onChange={(v) => set('show_thinking', v)}>
            사고 과정 표시
          </Toggle>
        </div>
      )}

      {tab === 'rag' && (
        <>
          <div className="card">
            <h3>기본 동작</h3>
            <div className="row wrap">
              <Toggle checked={!!val('rag_default_enabled')} onChange={(v) => set('rag_default_enabled', v)}>
                새 대화에서 RAG 기본 켜기
              </Toggle>
              <Toggle checked={!!val('rag_extract_graph')} onChange={(v) => set('rag_extract_graph', v)}>
                그래프 추출 (끄면 벡터 전용)
              </Toggle>
              <Toggle checked={!!val('rag_index_locked_files')} onChange={(v) => set('rag_index_locked_files', v)}>
                잠긴 파일도 색인
              </Toggle>
            </div>
            <Field label="기본 검색 모드">
              <select value={val('rag_default_mode')} onChange={(e) => set('rag_default_mode', e.target.value)}>
                <option value="hybrid">하이브리드 (권장)</option>
                <option value="naive">벡터만</option>
                <option value="local">엔티티 중심</option>
                <option value="global">관계 중심</option>
              </select>
            </Field>
          </div>

          <div className="card">
            <h3>감시 폴더 <span className="mute2">여기 넣은 문서가 자동 색인됩니다</span></h3>
            <ListEditor
              value={val('rag_watch_dirs') ?? []}
              onChange={(v) => set('rag_watch_dirs', v)}
              placeholder="documents"
            />
          </div>

          <div className="card">
            <h3>색인 파라미터</h3>
            <div className="grid2">
              <Field label="청크 크기 (자)"><input type="number" value={val('rag_chunk_size')} onChange={(e) => set('rag_chunk_size', Number(e.target.value))} /></Field>
              <Field label="청크 오버랩"><input type="number" value={val('rag_chunk_overlap')} onChange={(e) => set('rag_chunk_overlap', Number(e.target.value))} /></Field>
              <Field label="최대 파일 크기 (MB)"><input type="number" value={val('rag_max_file_mb')} onChange={(e) => set('rag_max_file_mb', Number(e.target.value))} /></Field>
            </div>
          </div>

          <div className="card">
            <h3>검색 파라미터</h3>
            <div className="grid2">
              <Field label="청크 top-k"><input type="number" value={val('rag_top_k_chunks')} onChange={(e) => set('rag_top_k_chunks', Number(e.target.value))} /></Field>
              <Field label="엔티티 top-k"><input type="number" value={val('rag_top_k_entities')} onChange={(e) => set('rag_top_k_entities', Number(e.target.value))} /></Field>
              <Field label="관계 top-k"><input type="number" value={val('rag_top_k_relations')} onChange={(e) => set('rag_top_k_relations', Number(e.target.value))} /></Field>
              <Field label="그래프 탐색 깊이" hint="0~2"><input type="number" value={val('rag_graph_depth')} onChange={(e) => set('rag_graph_depth', Number(e.target.value))} /></Field>
            </div>
          </div>

          <div className="card row">
            <div className="grow mute2">설정을 바꾼 뒤에는 다시 색인해야 반영됩니다.</div>
            <button onClick={() => run(() => api.post('/api/rag/reindex', {}), '전체 재인덱싱을 큐에 넣었습니다')}>
              전체 재인덱싱
            </button>
          </div>
        </>
      )}

      {tab === 'nas' && <NasTab val={val} set={set} />}

      {tab === 'security' && <SecurityTab val={val} set={set} />}

      {tab === 'personas' && <PersonasTab models={chatModels.map((m) => m.name)} />}
    </div>
  )
}

function ModelSelect({
  list,
  value,
  onChange,
}: {
  list: ModelInfo[]
  value: string
  onChange: (v: string) => void
}) {
  const known = list.some((m) => m.name === value)
  return (
    <select value={value ?? ''} onChange={(e) => onChange(e.target.value)}>
      {!known && value && <option value={value}>{value} (미설치)</option>}
      {list.map((m) => (
        <option key={m.name} value={m.name}>{m.name}</option>
      ))}
    </select>
  )
}

function ListEditor({
  value,
  onChange,
  placeholder,
}: {
  value: string[]
  onChange: (v: string[]) => void
  placeholder?: string
}) {
  const [draft, setDraft] = useState('')
  return (
    <>
      <div className="row wrap" style={{ marginBottom: 8 }}>
        {value.map((v) => (
          <span key={v} className="badge">
            {v}
            <button
              className="ghost sm"
              aria-label={`${v} 삭제`}
              style={{ padding: '0 2px' }}
              onClick={() => onChange(value.filter((x) => x !== v))}
            >
              ✕
            </button>
          </span>
        ))}
        {!value.length && <span className="mute2">비어 있음</span>}
      </div>
      <div className="row">
        <input
          className="grow"
          placeholder={placeholder}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && draft.trim()) {
              e.preventDefault()
              onChange([...new Set([...value, draft.trim()])])
              setDraft('')
            }
          }}
        />
        <button
          onClick={() => {
            if (!draft.trim()) return
            onChange([...new Set([...value, draft.trim()])])
            setDraft('')
          }}
        >
          추가
        </button>
      </div>
    </>
  )
}

function NasTab({ val, set }: { val: (k: string) => any; set: (k: string, v: any) => void }) {
  const run = useRun()
  const [usage, setUsage] = useState<any>(null)

  return (
    <>
      <div className="card">
        <h3>동작</h3>
        <div className="row wrap">
          <Toggle checked={!!val('nas_use_trash')} onChange={(v) => set('nas_use_trash', v)}>
            삭제 시 휴지통 사용
          </Toggle>
          <Toggle checked={!!val('nas_show_hidden_default')} onChange={(v) => set('nas_show_hidden_default', v)}>
            숨김 항목 기본 표시
          </Toggle>
        </div>
      </div>

      <div className="card">
        <h3>브라우저에서 바로 열 확장자</h3>
        <ListEditor value={val('nas_preview_exts') ?? []} onChange={(v) => set('nas_preview_exts', v)} placeholder=".pdf" />
      </div>

      <div className="card">
        <h3>용량</h3>
        {usage ? (
          <div className="grid2">
            <div><div className="mute2">NAS 파일</div><div>{fmtSize(usage.nas_bytes)} · {usage.nas_files}개</div></div>
            <div><div className="mute2">디스크 사용</div><div>{fmtSize(usage.disk_used)} / {fmtSize(usage.disk_total)}</div></div>
            <div><div className="mute2">남은 공간</div><div>{fmtSize(usage.disk_free)}</div></div>
          </div>
        ) : (
          <button onClick={async () => setUsage(await run(() => api.get<any>('/api/files/usage')))}>
            계산하기
          </button>
        )}
      </div>
    </>
  )
}

function SecurityTab({ val, set }: { val: (k: string) => any; set: (k: string, v: any) => void }) {
  const run = useRun()
  const [cur, setCur] = useState('')
  const [next, setNext] = useState('')
  const [flags, setFlags] = useState<any[]>([])

  const loadFlags = useCallback(async () => {
    const r = await run(() => api.get<{ flags: any[] }>('/api/settings/flags'))
    if (r) setFlags(r.flags)
  }, [run])

  useEffect(() => {
    loadFlags()
  }, [loadFlags])

  return (
    <>
      <div className="card">
        <h3>비밀번호 변경</h3>
        <div className="grid2">
          <Field label="현재 비밀번호">
            <input type="password" value={cur} onChange={(e) => setCur(e.target.value)} />
          </Field>
          <Field label="새 비밀번호">
            <input type="password" value={next} onChange={(e) => setNext(e.target.value)} />
          </Field>
        </div>
        <button
          disabled={!cur || !next}
          onClick={async () => {
            const ok = await run(
              () => api.post('/api/auth/password', { current_password: cur, new_password: next }),
              '비밀번호를 변경했습니다',
            )
            if (ok) {
              setCur('')
              setNext('')
            }
          }}
        >
          변경
        </button>
      </div>

      <div className="card">
        <h3>세션</h3>
        <div className="grid2">
          <Field label="로그인 유지 (일)">
            <input type="number" value={val('session_days')} onChange={(e) => set('session_days', Number(e.target.value))} />
          </Field>
          <Field label="파일 잠금 해제 유지 (분)">
            <input type="number" value={val('file_unlock_minutes')} onChange={(e) => set('file_unlock_minutes', Number(e.target.value))} />
          </Field>
        </div>
      </div>

      <div className="card flush">
        <table>
          <thead>
            <tr><th>숨김 · 잠금이 걸린 경로</th><th style={{ width: 100 }}>숨김</th><th style={{ width: 100 }}>잠금</th></tr>
          </thead>
          <tbody>
            {flags.map((f) => (
              <tr key={f.path}>
                <td className="mono">{f.path}</td>
                <td>{f.hidden ? <span className="badge">숨김</span> : '-'}</td>
                <td>{f.locked ? <span className="badge warn">잠김</span> : '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!flags.length && <div className="empty">설정된 항목이 없습니다</div>}
      </div>
    </>
  )
}

function PersonasTab({ models }: { models: string[] }) {
  const run = useRun()
  const toast = useToast()
  const [list, setList] = useState<Persona[]>([])
  const [edit, setEdit] = useState<Partial<Persona> | null>(null)

  const load = useCallback(async () => {
    const r = await run(() => api.get<{ personas: Persona[] }>('/api/personas'))
    if (r) setList(r.personas)
  }, [run])

  useEffect(() => {
    load()
  }, [load])

  const save = async () => {
    if (!edit?.name?.trim()) {
      toast('이름을 입력하세요', true)
      return
    }
    const body = {
      name: edit.name,
      system_prompt: edit.system_prompt ?? '',
      model: edit.model || null,
      temperature: edit.temperature ?? null,
      is_default: !!edit.is_default,
    }
    const ok = edit.id
      ? await run(() => api.patch(`/api/personas/${edit.id}`, body), '저장했습니다')
      : await run(() => api.post('/api/personas', body), '추가했습니다')
    if (ok) {
      setEdit(null)
      load()
    }
  }

  return (
    <>
      <div className="row" style={{ marginBottom: 12 }}>
        <div className="grow mute2">페르소나는 시스템 프롬프트 · 모델 · temperature 묶음입니다.</div>
        <button className="primary" onClick={() => setEdit({ name: '', system_prompt: '' })}>+ 페르소나</button>
      </div>

      {list.map((p) => (
        <div className="card" key={p.id}>
          <h3>
            {p.name}
            {p.is_default && <span className="badge on" style={{ marginLeft: 8 }}>기본</span>}
            <span className="right row">
              <button className="sm" onClick={() => setEdit(p)}>수정</button>
              <button
                className="sm danger"
                onClick={async () => {
                  if (!confirm(`'${p.name}' 을(를) 삭제할까요?`)) return
                  await run(() => api.del(`/api/personas/${p.id}`))
                  load()
                }}
              >
                삭제
              </button>
            </span>
          </h3>
          <div className="mute2" style={{ whiteSpace: 'pre-wrap' }}>{p.system_prompt || '(시스템 프롬프트 없음)'}</div>
          <div className="mute2" style={{ marginTop: 8 }}>
            {p.model || '기본 모델'} · temperature {p.temperature ?? '기본'}
          </div>
        </div>
      ))}

      {edit && (
        <Modal title={edit.id ? '페르소나 수정' : '새 페르소나'} onClose={() => setEdit(null)} onSubmit={save} submitLabel="저장">
          <Field label="이름">
            <input value={edit.name ?? ''} autoFocus onChange={(e) => setEdit({ ...edit, name: e.target.value })} />
          </Field>
          <Field label="시스템 프롬프트">
            <textarea
              rows={8}
              value={edit.system_prompt ?? ''}
              onChange={(e) => setEdit({ ...edit, system_prompt: e.target.value })}
            />
          </Field>
          <Field label="모델" hint="비우면 설정의 기본 모델">
            <select value={edit.model ?? ''} onChange={(e) => setEdit({ ...edit, model: e.target.value || null })}>
              <option value="">기본 모델</option>
              {models.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </Field>
          <Field label="temperature" hint="비우면 설정의 기본값">
            <input
              type="number" min={0} max={2} step={0.05}
              value={edit.temperature ?? ''}
              onChange={(e) => setEdit({ ...edit, temperature: e.target.value === '' ? null : Number(e.target.value) })}
            />
          </Field>
          <Toggle checked={!!edit.is_default} onChange={(v) => setEdit({ ...edit, is_default: v })}>
            기본 페르소나로 사용
          </Toggle>
        </Modal>
      )}
    </>
  )
}
