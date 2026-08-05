import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, fmtSize, fmtTime, uploadWithProgress, type FileItem, type Preview } from '../api'
import { Field, Modal, Toggle, useRun, useToast } from '../ui'

/** 진행 중인 작업 하나. 업로드는 바이트 진행률이 있고, 이동은 끝나면 100% 로 채운다. */
interface Job {
  name: string
  size: number
  loaded: number
  status: 'pending' | 'uploading' | 'done' | 'error'
  error?: string
}

const parentOf = (p: string) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '')

/** dest 가 item 자신이거나 그 하위면 옮길 수 없다 (백엔드도 막지만 UI 에서 먼저 걸러준다). */
const isInside = (dest: string, item: string) => dest === item || dest.startsWith(`${item}/`)

const contentUrl = (path: string, download = false) =>
  `/api/files/content?path=${encodeURIComponent(path)}${download ? '&download=1' : ''}`

interface Listing {
  path: string
  parent: string | null
  breadcrumbs: { name: string; path: string }[]
  show_hidden: boolean
  watched: boolean
  items: FileItem[]
}

type Dialog =
  | { kind: 'mkdir' }
  | { kind: 'rename'; item: FileItem }
  | { kind: 'move'; item: FileItem }  // 경로를 직접 치는 대신 폴더를 골라서 이동한다
  | { kind: 'lock'; item: FileItem }
  | { kind: 'unlock'; item: FileItem; then: 'preview' | 'download' }
  | null

const ICONS: Record<string, string> = {
  '.pdf': '📕', '.md': '📝', '.txt': '📄', '.docx': '📘', '.png': '🖼️', '.jpg': '🖼️',
  '.jpeg': '🖼️', '.gif': '🖼️', '.webp': '🖼️', '.svg': '🖼️', '.zip': '🗜️',
  '.mp4': '🎬', '.mov': '🎬', '.mp3': '🎵', '.json': '🧾', '.csv': '📊',
}

export default function Files() {
  const run = useRun()
  const toast = useToast()
  const [path, setPath] = useState('')
  const [data, setData] = useState<Listing | null>(null)
  const [showHidden, setShowHidden] = useState(false)
  const [dialog, setDialog] = useState<Dialog>(null)
  const [text, setText] = useState('')
  const [password, setPassword] = useState('')
  const [over, setOver] = useState(false)
  const [jobs, setJobs] = useState<Job[]>([])
  const [jobKind, setJobKind] = useState<'업로드' | '이동'>('업로드')
  const [drag, setDrag] = useState<FileItem | null>(null)
  const [dropTarget, setDropTarget] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const [trashOpen, setTrashOpen] = useState(false)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const load = useCallback(
    async (p = path, hidden = showHidden) => {
      const r = await run(() =>
        api.get<Listing>(`/api/files?path=${encodeURIComponent(p)}&show_hidden=${hidden ? 1 : 0}`),
      )
      if (r) setData(r)
    },
    [path, showHidden, run],
  )

  useEffect(() => {
    load(path, showHidden)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, showHidden])

  const go = (p: string) => setPath(p)

  const upload = async (files: FileList | File[]) => {
    const list = Array.from(files)
    if (!list.length) return

    setJobKind('업로드')
    setJobs(list.map((f) => ({ name: f.name, size: f.size, loaded: 0, status: 'pending' })))
    const ctrl = new AbortController()
    abortRef.current = ctrl

    const patch = (i: number, u: Partial<Job>) =>
      setJobs((prev) => prev.map((x, j) => (j === i ? { ...x, ...u } : x)))

    let ok = 0
    for (let i = 0; i < list.length; i++) {
      if (ctrl.signal.aborted) {
        patch(i, { status: 'error', error: '취소됨' })
        continue
      }
      patch(i, { status: 'uploading' })

      const form = new FormData()
      form.append('path', path)
      form.append('files', list[i])
      try {
        await uploadWithProgress(
          '/api/files/upload',
          form,
          (loaded) => patch(i, { loaded }),
          ctrl.signal,
        )
        patch(i, { status: 'done', loaded: list[i].size })
        ok++
      } catch (e: any) {
        patch(i, { status: 'error', error: e?.message || String(e) })
      }
    }

    abortRef.current = null
    if (ok) toast(`${ok}개 업로드 완료`)
    load()
  }

  /** 이동은 한 번의 요청이라 바이트 진행률이 없다. 업로드와 같은 카드로 보여주되
   *  건별로 대기 → 완료(100%)만 채운다. */
  const moveItems = async (items: FileItem[], dest: string) => {
    const targets = items.filter((it) => it.path !== dest && parentOf(it.path) !== dest)
    if (!targets.length) return

    setJobKind('이동')
    setJobs(targets.map((it) => ({ name: it.name, size: 1, loaded: 0, status: 'pending' })))

    const patch = (i: number, u: Partial<Job>) =>
      setJobs((prev) => prev.map((x, j) => (j === i ? { ...x, ...u } : x)))

    let ok = 0
    for (let i = 0; i < targets.length; i++) {
      patch(i, { status: 'uploading' })
      try {
        await api.post('/api/files/move', { path: targets[i].path, dest })
        patch(i, { status: 'done', loaded: 1 })
        ok++
      } catch (e: any) {
        patch(i, { status: 'error', error: e?.message || String(e) })
      }
    }
    if (ok) toast(`${ok}개를 '${dest || 'NAS'}' 로 옮겼습니다`)
    load()
  }

  const del = async (item: FileItem) => {
    if (!confirm(`'${item.name}' 을(를) 휴지통으로 보낼까요?`)) return
    await run(() => api.del('/api/files', { path: item.path }), '휴지통으로 이동했습니다')
    load()
  }

  const toggleHidden = async (item: FileItem) => {
    await run(() => api.put('/api/files/flags', { path: item.path, hidden: !item.hidden }))
    load()
  }

  const loadPreview = useCallback(
    async (path: string) => {
      setPreviewBusy(true)
      const r = await run(() => api.get<Preview>(`/api/files/preview?path=${encodeURIComponent(path)}`))
      setPreviewBusy(false)
      if (r) setPreview(r)
    },
    [run],
  )

  const openFile = (item: FileItem, download = false) => {
    if (item.locked) {
      setPassword('')
      setDialog({ kind: 'unlock', item, then: download ? 'download' : 'preview' })
      return
    }
    if (download) window.open(contentUrl(item.path, true), '_blank')
    else loadPreview(item.path)
  }

  const submitDialog = async () => {
    if (!dialog) return
    if (dialog.kind === 'mkdir') {
      await run(() => api.post('/api/files/mkdir', { path, name: text }), '폴더를 만들었습니다')
    } else if (dialog.kind === 'rename') {
      await run(() => api.post('/api/files/rename', { path: dialog.item.path, name: text }), '이름을 바꿨습니다')
    } else if (dialog.kind === 'lock') {
      await run(
        () =>
          api.put('/api/files/flags', {
            path: dialog.item.path,
            ...(password ? { lock_password: password } : { clear_lock: true }),
          }),
        password ? '비밀번호를 설정했습니다' : '잠금을 해제했습니다',
      )
    } else if (dialog.kind === 'unlock') {
      const ok = await run(() => api.post('/api/files/unlock', { path: dialog.item.path, password }))
      if (!ok) return
      if (dialog.then === 'download') window.open(contentUrl(dialog.item.path, true), '_blank')
      else loadPreview(dialog.item.path)
    }
    setDialog(null)
    setText('')
    setPassword('')
    load()
  }

  const busy = jobs.some((u) => u.status === 'pending' || u.status === 'uploading')
  const done = jobs.filter((u) => u.status === 'done').length
  const totalBytes = jobs.reduce((s, u) => s + u.size, 0)
  const loaded = jobs.reduce((s, u) => s + (u.status === 'done' ? u.size : u.loaded), 0)
  const overall = totalBytes ? Math.round((loaded / totalBytes) * 100) : 0
  const uploading = jobKind === '업로드'

  /** 폴더 행·상위 폴더·breadcrumb 에 공통으로 붙는 드롭 대상 핸들러. */
  const dropZone = (dest: string | null, canDrop = true) => ({
    onDragOver: (e: React.DragEvent) => {
      if (!drag || dest === null || !canDrop) return
      e.preventDefault()
      e.stopPropagation()
      setDropTarget(dest)
    },
    onDragLeave: () => setDropTarget((t) => (t === dest ? null : t)),
    onDrop: (e: React.DragEvent) => {
      if (!drag || dest === null || !canDrop) return
      e.preventDefault()
      e.stopPropagation()
      const item = drag
      setDrag(null)
      setDropTarget(null)
      moveItems([item], dest)
    },
  })

  return (
    <div
      className="page"
      onDragOver={(e) => {
        // 목록 안에서 끌고 다니는 중이면 업로드 하이라이트를 켜지 않는다
        if (!e.dataTransfer.types.includes('Files')) return
        e.preventDefault()
        setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setOver(false)
        if (e.dataTransfer.files.length) upload(e.dataTransfer.files)
      }}
    >
      <header className="row wrap">
        <div className="crumbs grow">
          <a
            onClick={() => go('')}
            className={dropTarget === '' ? 'drop-target' : ''}
            {...dropZone('')}
          >
            NAS
          </a>
          {data?.breadcrumbs.map((b) => (
            <span key={b.path}>
              <span className="sep"> / </span>
              <a
                onClick={() => go(b.path)}
                className={dropTarget === b.path ? 'drop-target' : ''}
                {...dropZone(b.path)}
              >
                {b.name}
              </a>
            </span>
          ))}
          {data?.watched && <span className="badge on" style={{ marginLeft: 8 }}>RAG 감시 중</span>}
        </div>

        <Toggle checked={showHidden} onChange={setShowHidden}>숨김 표시</Toggle>
        <button onClick={() => setTrashOpen(true)}>휴지통</button>
        <button onClick={() => { setText(''); setDialog({ kind: 'mkdir' }) }}>새 폴더</button>
        <button className="primary" onClick={() => fileRef.current?.click()} disabled={busy}>
          {busy ? '업로드 중…' : '업로드'}
        </button>
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={(e) => e.target.files && upload(e.target.files)}
        />
      </header>

      {jobs.length > 0 ? (
        <div className="card uploads">
          <h3>
            {jobKind}
            <span className="mute2">
              {done}/{jobs.length}
              {uploading && ` · ${fmtSize(loaded)} / ${fmtSize(totalBytes)}`}
            </span>
            <span className="right row">
              <strong>{overall}%</strong>
              {busy && uploading ? (
                <button className="sm danger" onClick={() => abortRef.current?.abort()}>취소</button>
              ) : (
                <button className="sm" onClick={() => setJobs([])} disabled={busy}>닫기</button>
              )}
            </span>
          </h3>

          <div className="bar-track" style={{ marginBottom: 12 }}>
            <div className="bar-fill" style={{ width: `${overall}%` }} />
          </div>

          {jobs.map((u, i) => {
            const pct = u.size ? Math.round((u.loaded / u.size) * 100) : u.status === 'done' ? 100 : 0
            return (
              <div className="up-row" key={`${u.name}-${i}`}>
                <span className="nm truncate" title={u.name}>{u.name}</span>
                <div className="bar-track grow">
                  <div
                    className={`bar-fill${u.status === 'error' ? ' err' : ''}`}
                    style={{ width: `${u.status === 'error' ? 100 : pct}%` }}
                  />
                </div>
                <span className="mute2 up-pct">
                  {u.status === 'error'
                    ? (u.error || '실패')
                    : u.status === 'done'
                      ? '완료'
                      : u.status === 'pending'
                        ? '대기'
                        : `${pct}%`}
                </span>
              </div>
            )
          })}
        </div>
      ) : (
        <div className={`drop${over ? ' over' : ''}`}>
          여기로 파일을 끌어다 놓으면 업로드됩니다
          <span className="mute2"> · 목록의 항목은 폴더나 위쪽 경로로 끌어다 옮길 수 있습니다</span>
        </div>
      )}

      <div className="card flush files">
        <table>
          <thead>
            <tr>
              <th>이름</th>
              <th style={{ width: 100 }}>크기</th>
              <th style={{ width: 150 }}>수정</th>
              <th style={{ width: 240 }} />
            </tr>
          </thead>
          <tbody>
            {data?.parent !== null && data && (
              <tr
                className={dropTarget === data.parent ? 'drop-target' : ''}
                {...dropZone(data.parent)}
              >
                <td className="name">
                  <span className="ico" aria-hidden="true">↰</span>
                  <span className="fname" onClick={() => go(data.parent!)}>상위 폴더</span>
                </td>
                <td colSpan={3} />
              </tr>
            )}
            {data?.items.map((it) => (
              <tr
                key={it.path}
                className={[
                  it.hidden ? 'is-hidden' : '',
                  drag?.path === it.path ? 'dragging' : '',
                  dropTarget === it.path ? 'drop-target' : '',
                ].filter(Boolean).join(' ')}
                draggable
                onDragStart={(e) => {
                  setDrag(it)
                  e.dataTransfer.effectAllowed = 'move'
                  e.dataTransfer.setData('text/plain', it.path)
                }}
                onDragEnd={() => { setDrag(null); setDropTarget(null) }}
                // 폴더에만 떨어뜨릴 수 있다. 자기 자신·자기 하위는 제외.
                {...dropZone(it.is_dir ? it.path : null, !!drag && !isInside(it.path, drag.path))}
              >
                <td className="name">
                  <span className="ico" aria-hidden="true">{it.is_dir ? '📁' : ICONS[it.ext] || '📄'}</span>
                  <span className="fname" onClick={() => (it.is_dir ? go(it.path) : openFile(it))}>
                    {it.name}
                  </span>
                  {it.locked && <span className="badge warn">잠김</span>}
                  {it.hidden && <span className="badge">숨김</span>}
                  {it.indexable && data?.watched && <span className="badge on">RAG</span>}
                </td>
                <td className="mute2">{it.is_dir ? '-' : fmtSize(it.size)}</td>
                <td className="mute2">{fmtTime(it.mtime)}</td>
                <td>
                  <span className="acts">
                    {!it.is_dir && (
                      <button className="ghost sm" aria-label={`${it.name} 다운로드`} onClick={() => openFile(it, true)}>다운로드</button>
                    )}
                    <button className="ghost sm" aria-label={`${it.name} 이름 변경`} onClick={() => { setText(it.name); setDialog({ kind: 'rename', item: it }) }}>이름</button>
                    <button className="ghost sm" aria-label={`${it.name} 이동`} onClick={() => { setText(''); setDialog({ kind: 'move', item: it }) }}>이동</button>
                    <button className="ghost sm" aria-label={`${it.name} ${it.hidden ? '숨김 해제' : '숨기기'}`} onClick={() => toggleHidden(it)}>
                      {it.hidden ? '숨김 해제' : '숨기기'}
                    </button>
                    {!it.is_dir && (
                      <button className="ghost sm" aria-label={`${it.name} 비밀번호 잠금`} onClick={() => { setPassword(''); setDialog({ kind: 'lock', item: it }) }}>잠금</button>
                    )}
                    <button className="ghost sm danger" aria-label={`${it.name} 삭제`} onClick={() => del(it)}>삭제</button>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && !data.items.length && <div className="empty">비어 있습니다</div>}
      </div>

      {dialog?.kind === 'mkdir' && (
        <Modal title="새 폴더" onClose={() => setDialog(null)} onSubmit={submitDialog} submitLabel="만들기">
          <Field label="폴더 이름">
            <input value={text} autoFocus onChange={(e) => setText(e.target.value)} />
          </Field>
        </Modal>
      )}

      {dialog?.kind === 'rename' && (
        <Modal title="이름 변경" onClose={() => setDialog(null)} onSubmit={submitDialog} submitLabel="변경">
          <Field label="새 이름">
            <input value={text} autoFocus onChange={(e) => setText(e.target.value)} />
          </Field>
        </Modal>
      )}

      {dialog?.kind === 'move' && (
        <FolderPicker
          item={dialog.item}
          start={path}
          onClose={() => setDialog(null)}
          onPick={(dest) => {
            setDialog(null)
            moveItems([dialog.item], dest)
          }}
        />
      )}

      {dialog?.kind === 'lock' && (
        <Modal
          title={`'${dialog.item.name}' 잠금`}
          onClose={() => setDialog(null)}
          onSubmit={submitDialog}
          submitLabel={password ? '잠그기' : '잠금 해제'}
        >
          <Field label="열람 비밀번호" hint="비우고 저장하면 잠금이 풀립니다">
            <input type="password" value={password} autoFocus onChange={(e) => setPassword(e.target.value)} />
          </Field>
          <p className="mute2">
            잠긴 파일은 목록에는 보이지만 열람 시 비밀번호를 요구합니다. 디스크의 파일 자체는 암호화되지
            않으므로 Finder/터미널 직접 접근은 막지 못합니다.
          </p>
        </Modal>
      )}

      {dialog?.kind === 'unlock' && (
        <Modal title="비밀번호 입력" onClose={() => setDialog(null)} onSubmit={submitDialog} submitLabel="열기">
          <Field label={dialog.item.name}>
            <input type="password" value={password} autoFocus onChange={(e) => setPassword(e.target.value)} />
          </Field>
        </Modal>
      )}

      {(preview || previewBusy) && (
        <PreviewPanel data={preview} busy={previewBusy} onClose={() => setPreview(null)} />
      )}

      {trashOpen && <Trash onClose={() => { setTrashOpen(false); load() }} />}
    </div>
  )
}

/** VS Code 의 폴더 고르기처럼, 경로를 치는 대신 들어가면서 고른다. */
function FolderPicker({
  item,
  start,
  onClose,
  onPick,
}: {
  item: FileItem
  start: string
  onClose: () => void
  onPick: (dest: string) => void
}) {
  const run = useRun()
  const [cur, setCur] = useState(start)
  const [data, setData] = useState<Listing | null>(null)
  const [busy, setBusy] = useState(false)
  const [newName, setNewName] = useState('')

  const load = useCallback(
    async (p: string) => {
      setBusy(true)
      const r = await run(() => api.get<Listing>(`/api/files?path=${encodeURIComponent(p)}&show_hidden=1`))
      setBusy(false)
      if (r) {
        setData(r)
        setCur(r.path)
      }
    },
    [run],
  )

  useEffect(() => { load(start) }, [load, start])

  const dirs = (data?.items ?? []).filter((x) => x.is_dir)
  const here = parentOf(item.path) === cur   // 이미 이 폴더에 있다
  const inSelf = isInside(cur, item.path)    // 자기 자신 안으로는 못 간다

  return (
    <Modal
      title={`'${item.name}' 이동`}
      onClose={onClose}
      onSubmit={() => onPick(cur)}
      submitLabel={here ? '같은 위치' : `여기로 이동`}
      submitDisabled={here || inSelf}
    >
      <div className="crumbs" style={{ marginBottom: 8 }}>
        <a onClick={() => load('')}>NAS</a>
        {data?.breadcrumbs.map((b) => (
          <span key={b.path}>
            <span className="sep"> / </span>
            <a onClick={() => load(b.path)}>{b.name}</a>
          </span>
        ))}
      </div>

      <div className="picker">
        {data?.parent !== null && data && (
          <div className="row-item" onClick={() => load(data.parent!)}>
            <span aria-hidden="true">↰</span> 상위 폴더
          </div>
        )}
        {dirs.map((d) => {
          const blocked = isInside(d.path, item.path)
          return (
            <div
              key={d.path}
              className={`row-item${blocked ? ' disabled' : ''}`}
              onClick={() => !blocked && load(d.path)}
              title={blocked ? '자기 자신 안으로는 옮길 수 없습니다' : d.path}
            >
              <span aria-hidden="true">📁</span>
              <span className="grow truncate">{d.name}</span>
              {d.hidden && <span className="badge">숨김</span>}
            </div>
          )
        })}
        {!busy && !dirs.length && data?.parent === null && (
          <div className="empty">하위 폴더가 없습니다</div>
        )}
      </div>

      <div className="row" style={{ marginTop: 10 }}>
        <input
          className="grow"
          placeholder="새 폴더 이름"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && e.preventDefault()}
        />
        <button
          type="button"
          disabled={!newName.trim()}
          onClick={async () => {
            const ok = await run(() => api.post('/api/files/mkdir', { path: cur, name: newName.trim() }))
            if (ok) {
              setNewName('')
              load(cur)
            }
          }}
        >
          폴더 만들기
        </button>
      </div>

      <div className="mute2" style={{ marginTop: 8 }}>
        목적지: <code>{cur || 'NAS (루트)'}</code>
        {here && ' — 이미 여기에 있습니다'}
        {inSelf && ' — 자기 자신 안으로는 옮길 수 없습니다'}
      </div>
    </Modal>
  )
}

function PreviewPanel({
  data,
  busy,
  onClose,
}: {
  data: Preview | null
  busy: boolean
  onClose: () => void
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <aside className="preview" aria-label="파일 미리보기">
      <header>
        <div className="grow truncate">
          <div className="title truncate">{data?.name ?? '불러오는 중…'}</div>
          {data && <div className="mute2">{fmtSize(data.size)} · {data.mime}</div>}
        </div>
        {data && (
          <a className="badge" href={contentUrl(data.path, true)} target="_blank" rel="noreferrer">
            다운로드
          </a>
        )}
        <button className="ghost sm" aria-label="미리보기 닫기" onClick={onClose}>닫기</button>
      </header>

      <div className="body">
        {busy && <div className="empty">불러오는 중…</div>}

        {data?.kind === 'image' && (
          <img src={contentUrl(data.path)} alt={data.name} />
        )}

        {data?.kind === 'pdf' && (
          <iframe src={contentUrl(data.path)} title={data.name} />
        )}

        {data?.kind === 'markdown' && (
          <div className="md">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.text || ''}</ReactMarkdown>
          </div>
        )}

        {data?.kind === 'text' && <pre className="mono">{data.text}</pre>}

        {data?.kind === 'none' && !busy && (
          <div className="empty">
            이 형식은 인라인 미리보기를 지원하지 않습니다.
            {data.error && <div className="mute2" style={{ marginTop: 8 }}>{data.error}</div>}
            <div style={{ marginTop: 12 }}>
              <a className="badge" href={contentUrl(data.path, true)} target="_blank" rel="noreferrer">
                다운로드
              </a>
            </div>
          </div>
        )}

        {data?.truncated && (
          <div className="mute2" style={{ padding: 12 }}>
            길어서 앞부분만 표시했습니다. 전체는 다운로드해서 확인하세요.
          </div>
        )}
      </div>
    </aside>
  )
}

function Trash({ onClose }: { onClose: () => void }) {
  const run = useRun()
  const [items, setItems] = useState<any[]>([])

  const load = useCallback(async () => {
    const r = await run(() => api.get<{ items: any[] }>('/api/files/trash'))
    if (r) setItems(r.items)
  }, [run])

  useEffect(() => {
    load()
  }, [load])

  return (
    <Modal title="휴지통" onClose={onClose}>
      {!items.length && <div className="empty">비어 있습니다</div>}
      {items.map((it) => (
        <div key={it.name} className="row" style={{ padding: '6px 0', borderBottom: '1px solid var(--line)' }}>
          <div className="grow truncate">
            <div className="truncate">{it.original}</div>
            <div className="mute2">{it.deleted_at} · {fmtSize(it.size)}</div>
          </div>
          <button
            className="sm"
            onClick={async () => {
              await run(() => api.post('/api/files/trash/restore', { name: it.name }), '복원했습니다')
              load()
            }}
          >
            복원
          </button>
          <button
            className="sm danger"
            onClick={async () => {
              if (!confirm('영구 삭제할까요?')) return
              await run(() => api.del('/api/files/trash', { name: it.name }))
              load()
            }}
          >
            삭제
          </button>
        </div>
      ))}
      {!!items.length && (
        <button
          className="danger"
          style={{ width: '100%', marginTop: 12 }}
          onClick={async () => {
            if (!confirm('휴지통을 비울까요? 되돌릴 수 없습니다.')) return
            await run(() => api.del('/api/files/trash'), '휴지통을 비웠습니다')
            load()
          }}
        >
          휴지통 비우기
        </button>
      )}
    </Modal>
  )
}
