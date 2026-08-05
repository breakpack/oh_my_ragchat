import { useCallback, useEffect, useState } from 'react'
import { api, fmtTime } from '../api'
import { Field, useRun, useToast } from '../ui'
import { useRagEvents } from '../useRagEvents'
import { GraphView } from './Knowledge'

interface NotionStatus {
  configured: boolean
  token_source: 'env' | 'db' | ''
  token_masked: string
  stats: { pages: number; ready: number; errors: number }
  queued: number
}

interface NotionPage {
  id: number
  path: string
  url: string | null
  status: string
  chunk_count: number
  error: string | null
  indexed_at: string | null
  progress_done: number
  progress_total: number
  phase: string | null
  entity_count: number
}

const STATUS: Record<string, { label: string; cls: string }> = {
  pending: { label: '대기', cls: '' },
  embedding: { label: '임베딩 중', cls: 'warn' },
  graphing: { label: '그래프 구축 중', cls: 'warn' },
  ready: { label: '완료', cls: 'ok' },
  error: { label: '실패', cls: 'err' },
  skipped: { label: '건너뜀', cls: '' },
}

export default function Notion() {
  const [tab, setTab] = useState<'pages' | 'graph'>('pages')
  return (
    <div className="page">
      <header>
        <h2>Notion</h2>
        <div className="mute2">
          페이지 링크를 넣으면 하위 페이지까지 따라가며 읽고 같은 그래프에 얹습니다.
        </div>
      </header>

      <div className="tabs">
        <button className={tab === 'pages' ? 'active' : ''} onClick={() => setTab('pages')}>페이지</button>
        <button className={tab === 'graph' ? 'active' : ''} onClick={() => setTab('graph')}>그래프</button>
      </div>

      {tab === 'pages' ? <Pages /> : <GraphView source="notion" />}
    </div>
  )
}

function Pages() {
  const run = useRun()
  const toast = useToast()
  const [st, setSt] = useState<NotionStatus | null>(null)
  const [pages, setPages] = useState<NotionPage[]>([])
  const [url, setUrl] = useState('')
  const [depth, setDepth] = useState(3)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    const s = await run(() => api.get<NotionStatus>('/api/notion/status'))
    if (s) setSt(s)
    const p = await run(() => api.get<{ pages: NotionPage[] }>('/api/notion/pages'))
    if (p) setPages(p.pages)
  }, [run])

  useEffect(() => {
    load()
  }, [load])

  // 색인 진행률은 NAS 문서와 같은 SSE 로 흘러온다
  const live = useRagEvents((ev) => {
    if (!ev.path?.startsWith('notion')) {
      if (ev.kind === 'document') load()
      return
    }
    if (ev.kind === 'document') {
      load()
      return
    }
    setPages((prev) =>
      prev.map((p) =>
        (ev.document_id && p.id === ev.document_id) || p.path === ev.path
          ? { ...p, status: ev.status ?? p.status, progress_done: ev.done ?? 0,
              progress_total: ev.total ?? 0, phase: ev.phase ?? p.phase }
          : p,
      ),
    )
  })

  useEffect(() => {
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [load])

  const crawl = async () => {
    if (!url.trim()) return
    setBusy(true)
    const r = await run(() => api.post<any>('/api/notion/crawl', { url: url.trim(), max_depth: depth }))
    setBusy(false)
    if (r) {
      toast('크롤을 큐에 넣었습니다')
      setUrl('')
      load()
    }
  }

  return (
    <>
      <TokenCard st={st} onChange={load} />

      {st?.configured && (
        <div className="card">
          <h3>페이지 크롤<span className="mute2">하위 페이지를 따라가며 읽습니다</span></h3>
          <div className="row wrap">
            <input
              className="grow"
              style={{ minWidth: 260 }}
              placeholder="https://www.notion.so/... (통합에 연결된 페이지)"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && crawl()}
            />
            <select style={{ width: 130 }} value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
              {[0, 1, 2, 3, 4, 5].map((d) => (
                <option key={d} value={d}>깊이 {d}</option>
              ))}
            </select>
            <button className="primary" onClick={crawl} disabled={busy}>
              {busy ? '…' : '크롤 시작'}
            </button>
          </div>
          <div className="mute2" style={{ marginTop: 8 }}>
            Notion 에서 <b>해당 페이지를 통합(integration)에 연결</b>해야 읽을 수 있습니다.
            페이지 우측 상단 ··· → 연결 → 만든 통합 선택.
          </div>
        </div>
      )}

      <div className="row wrap" style={{ marginBottom: 12 }}>
        <span className="live">
          <span className={`dot ${live ? 'ok' : 'warn'}`} />
          {live ? '실시간' : '30초 갱신'}
        </span>
        {st && (
          <span className="mute2">
            페이지 {st.stats.ready}/{st.stats.pages} 완료
            {st.stats.errors > 0 && ` · 실패 ${st.stats.errors}`}
            {st.queued > 0 && ` · 큐 ${st.queued}`}
          </span>
        )}
        <button
          className="right danger"
          onClick={async () => {
            if (!confirm('Notion 색인을 전부 지울까요? NAS 문서는 그대로 둡니다.')) return
            await run(() => api.del('/api/notion/pages'), '지웠습니다')
            load()
          }}
        >
          Notion 색인 비우기
        </button>
      </div>

      <div className="card flush">
        <table>
          <thead>
            <tr>
              <th>페이지</th>
              <th style={{ width: 150 }}>상태</th>
              <th style={{ width: 70 }}>청크</th>
              <th style={{ width: 80 }}>엔티티</th>
              <th style={{ width: 150 }}>색인 시각</th>
            </tr>
          </thead>
          <tbody>
            {pages.map((p) => {
              const s = STATUS[p.status] || { label: p.status, cls: '' }
              return (
                <tr key={p.id}>
                  <td className="truncate" style={{ maxWidth: 420 }} title={p.error || p.path}>
                    {p.url ? (
                      <a href={p.url} target="_blank" rel="noreferrer">{p.path.replace(/^notion\//, '')}</a>
                    ) : (
                      p.path.replace(/^notion\//, '')
                    )}
                    {p.error && <div className="mute2" style={{ color: 'var(--err)' }}>{p.error}</div>}
                  </td>
                  <td>
                    <span className={`badge ${s.cls}`}>{s.label}</span>
                    {p.progress_total > 0 && (
                      <div style={{ marginTop: 6 }}>
                        <div className="bar-track">
                          <div className="bar-fill" style={{ width: `${Math.round((p.progress_done / p.progress_total) * 100)}%` }} />
                        </div>
                        <div className="mute2" style={{ marginTop: 2 }}>
                          {p.progress_done}/{p.progress_total}
                        </div>
                      </div>
                    )}
                  </td>
                  <td className="mute2">{p.chunk_count}</td>
                  <td className="mute2">{p.entity_count}</td>
                  <td className="mute2">{fmtTime(p.indexed_at)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
        {!pages.length && (
          <div className="empty">
            아직 읽은 페이지가 없습니다. 위에 Notion 페이지 링크를 넣어보세요.
          </div>
        )}
      </div>
    </>
  )
}

function TokenCard({ st, onChange }: { st: NotionStatus | null; onChange: () => void }) {
  const run = useRun()
  const toast = useToast()
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)

  const save = async (token: string) => {
    setBusy(true)
    const ok = await run(
      () => api.put('/api/notion/token', { token }),
      token ? '토큰을 저장했습니다' : '토큰을 삭제했습니다',
    )
    setBusy(false)
    if (ok) {
      setDraft('')
      onChange()
    }
  }

  return (
    <div className="card">
      <h3>
        연결
        {st?.configured && <span className="badge ok">토큰 설정됨</span>}
        {st?.configured && <span className="mono mute2">{st.token_masked}</span>}
        {st?.configured && (
          <button
            className="sm right"
            onClick={async () => {
              const r = await run(() => api.post<any>('/api/notion/test'))
              if (r) toast(r.ok ? `연결 성공 (${r.bot || 'ok'})` : `실패: ${r.error}`, !r.ok)
            }}
          >
            연결 테스트
          </button>
        )}
      </h3>

      {st?.token_source === 'env' ? (
        <div className="mute2">.env 의 NOTION_TOKEN 으로 지정돼 있어 웹에서 바꿀 수 없습니다.</div>
      ) : (
        <>
          <Field
            label="내부 통합 토큰"
            hint="notion.so/my-integrations 에서 만든 Internal Integration Secret"
          >
            <div className="row">
              <input
                className="grow"
                type="password"
                autoComplete="off"
                placeholder="ntn_... 또는 secret_..."
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && draft.trim() && save(draft.trim())}
              />
              <button className="primary" disabled={busy || !draft.trim()} onClick={() => save(draft.trim())}>
                저장
              </button>
              {st?.configured && (
                <button className="danger" disabled={busy} onClick={() => save('')}>삭제</button>
              )}
            </div>
          </Field>
          <div className="mute2">
            토큰은 서버 DB 에 저장되고 조회 API 로는 되돌려주지 않습니다(마스킹만).
            읽으려는 페이지마다 <b>··· → 연결</b>에서 이 통합을 추가해야 합니다.
          </div>
        </>
      )}
    </div>
  )
}
