import { useCallback, useEffect, useState } from 'react'
import { api, fmtTime } from '../api'
import { useRun } from '../ui'

export default function JobsPage() {
  return (
    <div className="page">
      <header>
        <h2>작업 큐</h2>
        <div className="mute2">색인 잡의 진행·실패 상태입니다. 4초마다 갱신됩니다.</div>
      </header>
      <Jobs />
    </div>
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
