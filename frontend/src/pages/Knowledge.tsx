import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, fmtTime, type Citation, type DocRow, type RagEvent } from '../api'
import { Toggle, useRun, useToast } from '../ui'
import GraphCanvas, { type GEdge, type GNode } from '../components/GraphCanvas'

const STATUS: Record<string, { label: string; cls: string }> = {
  pending: { label: '대기', cls: '' },
  extracting: { label: '추출 중', cls: 'warn' },
  embedding: { label: '임베딩 중', cls: 'warn' },
  graphing: { label: '그래프 구축 중', cls: 'warn' },
  ready: { label: '완료', cls: 'ok' },
  error: { label: '실패', cls: 'err' },
  skipped: { label: '건너뜀', cls: '' },
}

export default function Knowledge() {
  const [tab, setTab] = useState<'docs' | 'search' | 'graph' | 'jobs'>('docs')
  return (
    <div className="page">
      <header>
        <h2>지식 베이스</h2>
        <div className="mute2">감시 폴더의 문서를 그래프 RAG 로 색인합니다.</div>
      </header>

      <div className="tabs">
        <button className={tab === 'docs' ? 'active' : ''} onClick={() => setTab('docs')}>문서</button>
        <button className={tab === 'search' ? 'active' : ''} onClick={() => setTab('search')}>검색 테스트</button>
        <button className={tab === 'graph' ? 'active' : ''} onClick={() => setTab('graph')}>그래프</button>
        <button className={tab === 'jobs' ? 'active' : ''} onClick={() => setTab('jobs')}>작업 큐</button>
      </div>

      {tab === 'docs' && <Documents />}
      {tab === 'search' && <SearchTest />}
      {tab === 'graph' && <GraphView />}
      {tab === 'jobs' && <Jobs />}
    </div>
  )
}

const PHASES: Record<string, string> = {
  ocr: 'OCR',
  embedding: '임베딩',
  graphing: '그래프',
}

function Documents() {
  const run = useRun()
  const toast = useToast()
  const [docs, setDocs] = useState<DocRow[]>([])
  const [stats, setStats] = useState<any>(null)
  const [q, setQ] = useState('')
  const [live, setLive] = useState(false)

  const load = useCallback(async () => {
    const r = await run(() =>
      api.get<{ documents: DocRow[] }>(
        `/api/rag/documents?limit=500${q ? `&q=${encodeURIComponent(q)}` : ''}`,
      ),
    )
    if (r) setDocs(r.documents)
    const s = await run(() => api.get<{ stats: any }>('/api/rag/stats'))
    if (s) setStats(s.stats)
  }, [run, q])

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q])

  // 워커가 Postgres NOTIFY 로 밀어주는 진행률을 그대로 받는다. 폴링은 안전망으로만 남긴다.
  useEffect(() => {
    let es: EventSource | null = null
    let refreshTimer: ReturnType<typeof setTimeout> | null = null
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    let attempt = 0
    let disposed = false

    const refreshSoon = () => {
      if (refreshTimer) return
      refreshTimer = setTimeout(() => {
        refreshTimer = null
        load()
      }, 1200)
    }

    const onMessage = (e: MessageEvent) => {
      let ev: RagEvent
      try {
        ev = JSON.parse(e.data)
      } catch {
        return
      }

      if (ev.kind === 'scan') {
        if (ev.queued || ev.removed) refreshSoon()
        return
      }
      if (ev.status === 'deleted' || ev.kind === 'document') refreshSoon()

      setDocs((prev) => {
        const i = prev.findIndex(
          (d) => (ev.document_id && d.id === ev.document_id) || d.path === ev.path,
        )
        if (i === -1) return prev
        const next = [...prev]
        next[i] = {
          ...next[i],
          status: ev.status ?? next[i].status,
          progress_done: ev.done ?? 0,
          progress_total: ev.total ?? 0,
          phase: ev.phase ?? next[i].phase,
          error: ev.error ?? (ev.status === 'error' ? next[i].error : null),
          chunk_count: ev.chunk_count ?? next[i].chunk_count,
        }
        return next
      })
    }

    const connect = () => {
      if (disposed) return
      es = new EventSource('/api/rag/events')
      es.onopen = () => {
        attempt = 0
        setLive(true)
      }
      es.onmessage = onMessage
      es.onerror = () => {
        setLive(false)
        // 502 처럼 치명적인 응답을 받으면 브라우저는 재연결을 포기하고 CLOSED 로 둔다
        // (api 컨테이너 재시작 중에 실제로 발생). 그때는 직접 다시 연다.
        if (es && es.readyState === EventSource.CLOSED) {
          es.close()
          es = null
          const delay = Math.min(2000 * 2 ** attempt++, 30000)
          retryTimer = setTimeout(connect, delay)
        }
      }
    }
    connect()

    const poll = setInterval(load, 30000) // SSE 가 끊겨도 화면이 굳지 않게
    return () => {
      disposed = true
      es?.close()
      clearInterval(poll)
      if (refreshTimer) clearTimeout(refreshTimer)
      if (retryTimer) clearTimeout(retryTimer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q])

  return (
    <>
      {stats && (
        <div className="card">
          <div className="grid2">
            <Stat label="문서" value={`${stats.ready} / ${stats.documents}`} hint="완료 / 전체" />
            <Stat label="청크" value={stats.chunks} />
            <Stat label="엔티티" value={stats.entities} />
            <Stat label="관계" value={stats.relations} />
          </div>
        </div>
      )}

      <div className="row wrap" style={{ marginBottom: 12 }}>
        <input
          className="grow"
          style={{ maxWidth: 320 }}
          placeholder="경로 검색"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <span
          className="live"
          title={
            live
              ? '워커와 실시간으로 연결돼 진행률이 즉시 갱신됩니다'
              : '실시간 연결이 끊겨 30초마다 갱신합니다. 색인 작업 자체는 계속 진행됩니다'
          }
        >
          <span className={`dot ${live ? 'ok' : 'warn'}`} />
          {live ? '실시간' : '30초 갱신'}
        </span>
        <button onClick={async () => { const r = await run(() => api.post<any>('/api/rag/scan')); if (r) { toast(`스캔: 신규 ${r.queued}건, 제거 ${r.removed}건`); load() } }}>
          지금 스캔
        </button>
        <button onClick={async () => { await run(() => api.post('/api/rag/reindex', {}), '전체 재인덱싱을 큐에 넣었습니다'); load() }}>
          전체 재인덱싱
        </button>
      </div>

      <div className="card flush">
        <table>
          <thead>
            <tr>
              <th>경로</th>
              <th style={{ width: 150 }}>상태</th>
              <th style={{ width: 70 }}>청크</th>
              <th style={{ width: 80 }}>엔티티</th>
              <th style={{ width: 150 }}>색인 시각</th>
              <th style={{ width: 170 }} />
            </tr>
          </thead>
          <tbody>
            {docs.map((d) => {
              const st = STATUS[d.status] || { label: d.status, cls: '' }
              return (
                <tr key={d.id}>
                  <td className="truncate" style={{ maxWidth: 380 }} title={d.error || d.path}>
                    <span className="mono">{d.path}</span>
                    {d.ocr && <span className="badge" style={{ marginLeft: 6 }}>OCR</span>}
                    {d.error && <div className="mute2" style={{ color: 'var(--err)' }}>{d.error}</div>}
                  </td>
                  <td>
                    <span className={`badge ${st.cls}`}>{st.label}</span>
                    {d.progress_total > 0 && (
                      <div style={{ marginTop: 6 }}>
                        <div className="bar-track">
                          <div
                            className="bar-fill"
                            style={{ width: `${Math.round((d.progress_done / d.progress_total) * 100)}%` }}
                          />
                        </div>
                        <div className="mute2" style={{ marginTop: 2 }}>
                          {PHASES[d.phase || ''] || ''} {d.progress_done}/{d.progress_total}
                        </div>
                      </div>
                    )}
                  </td>
                  <td className="mute2">{d.chunk_count}</td>
                  <td className="mute2">{d.entity_count}</td>
                  <td className="mute2">{fmtTime(d.indexed_at)}</td>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <button className="ghost sm" aria-label={`${d.path} 재색인`} onClick={async () => { await run(() => api.post('/api/rag/reindex', { path: d.path }), '재인덱싱을 큐에 넣었습니다'); load() }}>
                      재색인
                    </button>
                    <button className="ghost sm danger" aria-label={`${d.path} 색인 삭제`} onClick={async () => { await run(() => api.del(`/api/rag/documents/${d.id}`)); load() }}>
                      삭제
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        {!docs.length && (
          <div className="empty">
            색인된 문서가 없습니다.
            <br />
            <span className="mute2">파일 탭에서 감시 폴더(기본 documents/)에 문서를 넣어보세요.</span>
          </div>
        )}
      </div>
    </>
  )
}

function Stat({ label, value, hint }: { label: string; value: any; hint?: string }) {
  return (
    <div>
      <div className="mute2">{label}{hint && ` · ${hint}`}</div>
      <div className="stat-value">{value ?? '-'}</div>
    </div>
  )
}

function SearchTest() {
  const run = useRun()
  const [q, setQ] = useState('')
  const [mode, setMode] = useState('hybrid')
  const [result, setResult] = useState<any>(null)
  const [busy, setBusy] = useState(false)

  const search = async () => {
    if (!q.trim()) return
    setBusy(true)
    const r = await run(() =>
      api.get<any>(`/api/rag/search?q=${encodeURIComponent(q)}&mode=${mode}`),
    )
    setBusy(false)
    if (r) setResult(r)
  }

  return (
    <>
      <div className="card">
        <div className="row">
          <input
            className="grow"
            placeholder="검색어를 넣고 어떤 근거가 잡히는지 확인하세요"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && search()}
          />
          <select style={{ width: 150 }} value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="hybrid">하이브리드</option>
            <option value="naive">벡터만</option>
            <option value="local">엔티티 중심</option>
            <option value="global">관계 중심</option>
          </select>
          <button className="primary" onClick={search} disabled={busy}>{busy ? '검색 중…' : '검색'}</button>
        </div>
      </div>

      {result && (
        <>
          <div className="card">
            <h3>통계</h3>
            <div className="mono mute2">{JSON.stringify(result.stats)}</div>
          </div>
          {result.citations.map((c: Citation) => (
            <div className="card" key={c.chunk_id}>
              <h3>
                [{c.tag}] <span className="mono mute2">{c.path}</span>
                <span className="badge right">{c.score}</span>
              </h3>
              <div style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>{c.excerpt}</div>
            </div>
          ))}
          {result.empty && <div className="empty">근거를 찾지 못했습니다</div>}
          {result.prompt_block && (
            <details className="card">
              <summary className="mute2">LLM 에 전달되는 컨텍스트 원문</summary>
              <pre className="mono" style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{result.prompt_block}</pre>
            </details>
          )}
        </>
      )}
    </>
  )
}

function GraphView() {
  const run = useRun()
  const [entity, setEntity] = useState('')
  const [limit, setLimit] = useState(150)
  const [depth, setDepth] = useState(1)
  const [labels, setLabels] = useState(true)
  const [data, setData] = useState<{ nodes: GNode[]; edges: GEdge[]; seed: string | null } | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(
    async (name?: string, opts?: { limit?: number; depth?: number }) => {
      setBusy(true)
      const l = opts?.limit ?? limit
      const d = opts?.depth ?? depth
      const r = await run(() =>
        api.get<any>(
          `/api/rag/graph?depth=${d}&limit=${l}${name ? `&entity=${encodeURIComponent(name)}` : ''}`,
        ),
      )
      setBusy(false)
      if (r) setData(r)
    },
    [run, limit, depth],
  )

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const nodes = data?.nodes ?? []
  const edges = data?.edges ?? []
  const focusId = useMemo(() => {
    if (!entity) return null
    const hit = nodes.find((n) => n.name === entity)
    return hit?.id ?? null
  }, [entity, nodes])

  return (
    <>
      <div className="card">
        <div className="row wrap">
          <input
            className="grow"
            style={{ minWidth: 220 }}
            placeholder="엔티티 이름 (비우면 연결이 많은 노드부터)"
            value={entity}
            onChange={(e) => setEntity(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && load(entity)}
          />
          <select style={{ width: 120 }} value={depth} onChange={(e) => { const v = Number(e.target.value); setDepth(v); load(entity || undefined, { depth: v }) }}>
            <option value={1}>1홉</option>
            <option value={2}>2홉</option>
          </select>
          <select style={{ width: 130 }} value={limit} onChange={(e) => { const v = Number(e.target.value); setLimit(v); load(entity || undefined, { limit: v }) }}>
            {[60, 150, 300, 600].map((v) => (
              <option key={v} value={v}>{v}개</option>
            ))}
          </select>
          <Toggle checked={labels} onChange={setLabels}>라벨</Toggle>
          <button className="primary" onClick={() => load(entity)} disabled={busy}>
            {busy ? '불러오는 중…' : '보기'}
          </button>
        </div>
        <div className="mute2" style={{ marginTop: 8 }}>
          휠로 확대·축소, 빈 곳을 끌어 이동, 노드를 끌어 배치, 더블클릭하면 그 노드를 중심으로 다시 봅니다.
        </div>
      </div>

      {!nodes.length ? (
        <div className="empty">
          {busy ? '불러오는 중…' : '그래프가 비어 있습니다. 문서를 색인하면 엔티티가 생깁니다.'}
        </div>
      ) : (
        <>
          <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
            <GraphCanvas
              nodes={nodes}
              edges={edges}
              height={560}
              focusId={focusId}
              showLabels={labels}
              onOpen={(n) => {
                setEntity(n.name)
                load(n.name)
              }}
            />
          </div>

          <div className="row wrap mute2" style={{ margin: '0 0 12px' }}>
            <span>노드 {nodes.length}개 · 관계 {edges.length}개</span>
            {data?.seed && <span className="badge on">중심: {data.seed}</span>}
          </div>

          <div className="card flush">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 200 }}>엔티티</th>
                  <th style={{ width: 80 }}>유형</th>
                  <th style={{ width: 60 }}>연결</th>
                  <th>설명</th>
                </tr>
              </thead>
              <tbody>
                {[...nodes].sort((a, b) => (b.degree || 0) - (a.degree || 0)).slice(0, 30).map((n) => (
                  <tr key={n.id} style={{ cursor: 'pointer' }} onClick={() => { setEntity(n.name); load(n.name) }}>
                    <td>{n.name}</td>
                    <td className="mute2">{n.type}</td>
                    <td className="mute2">{n.degree}</td>
                    <td className="mute2 truncate" style={{ maxWidth: 420 }}>{n.description}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}

function Jobs() {
  const run = useRun()
  const [data, setData] = useState<any>(null)

  const load = useCallback(async () => {
    const r = await run(() => api.get<any>('/api/rag/jobs?limit=100'))
    if (r) setData(r)
  }, [run])

  useEffect(() => {
    load()
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [load])

  return (
    <>
      <div className="card row wrap">
        {Object.entries(data?.stats ?? {}).map(([k, v]) => (
          <span key={k} className={`badge ${k === 'failed' ? 'err' : k === 'running' ? 'warn' : ''}`}>
            {k} {String(v)}
          </span>
        ))}
        <div className="right row">
          <button onClick={async () => { await run(() => api.post('/api/rag/jobs/retry'), '실패한 작업을 다시 큐에 넣었습니다'); load() }}>
            실패 재시도
          </button>
          <button onClick={async () => { await run(() => api.del('/api/rag/jobs')); load() }}>완료 기록 정리</button>
        </div>
      </div>

      <div className="card flush">
        <table>
          <thead>
            <tr>
              <th style={{ width: 60 }}>#</th>
              <th style={{ width: 140 }}>종류</th>
              <th>대상</th>
              <th style={{ width: 90 }}>상태</th>
              <th style={{ width: 60 }}>시도</th>
              <th style={{ width: 150 }}>완료</th>
            </tr>
          </thead>
          <tbody>
            {(data?.jobs ?? []).map((j: any) => (
              <tr key={j.id}>
                <td className="mute2">{j.id}</td>
                <td className="mono">{j.kind}</td>
                <td className="mono truncate" style={{ maxWidth: 320 }} title={j.error || ''}>
                  {j.payload?.path || '-'}
                  {j.error && <div className="mute2" style={{ color: 'var(--err)' }}>{j.error}</div>}
                </td>
                <td>
                  <span className={`badge ${j.status === 'failed' ? 'err' : j.status === 'done' ? 'ok' : 'warn'}`}>
                    {j.status}
                  </span>
                </td>
                <td className="mute2">{j.attempts}</td>
                <td className="mute2">{fmtTime(j.done_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!data?.jobs?.length && <div className="empty">작업 기록이 없습니다</div>}
      </div>
    </>
  )
}
