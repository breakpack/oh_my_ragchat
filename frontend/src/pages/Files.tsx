import { useCallback, useEffect, useRef, useState } from 'react'
import { api, fmtSize, fmtTime, type FileItem } from '../api'
import { Field, Modal, Toggle, useRun, useToast } from '../ui'

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
  | { kind: 'move'; item: FileItem }
  | { kind: 'lock'; item: FileItem }
  | { kind: 'unlock'; item: FileItem; then: 'open' | 'download' }
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
  const [uploading, setUploading] = useState(false)
  const [trashOpen, setTrashOpen] = useState(false)
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
    const form = new FormData()
    form.append('path', path)
    Array.from(files).forEach((f) => form.append('files', f))
    setUploading(true)
    const r = await run(() => api.upload<{ saved: any[] }>('/api/files/upload', form))
    setUploading(false)
    if (r) {
      toast(`${r.saved.length}개 업로드 완료`)
      load()
    }
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

  const openFile = (item: FileItem, download = false) => {
    if (item.locked) {
      setPassword('')
      setDialog({ kind: 'unlock', item, then: download ? 'download' : 'open' })
      return
    }
    window.open(
      `/api/files/content?path=${encodeURIComponent(item.path)}${download ? '&download=1' : ''}`,
      '_blank',
    )
  }

  const submitDialog = async () => {
    if (!dialog) return
    if (dialog.kind === 'mkdir') {
      await run(() => api.post('/api/files/mkdir', { path, name: text }), '폴더를 만들었습니다')
    } else if (dialog.kind === 'rename') {
      await run(() => api.post('/api/files/rename', { path: dialog.item.path, name: text }))
    } else if (dialog.kind === 'move') {
      await run(() => api.post('/api/files/move', { path: dialog.item.path, dest: text }))
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
      window.open(
        `/api/files/content?path=${encodeURIComponent(dialog.item.path)}${
          dialog.then === 'download' ? '&download=1' : ''
        }`,
        '_blank',
      )
    }
    setDialog(null)
    setText('')
    setPassword('')
    load()
  }

  return (
    <div
      className="page"
      onDragOver={(e) => {
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
          <a onClick={() => go('')}>NAS</a>
          {data?.breadcrumbs.map((b) => (
            <span key={b.path}>
              <span className="sep"> / </span>
              <a onClick={() => go(b.path)}>{b.name}</a>
            </span>
          ))}
          {data?.watched && <span className="badge on" style={{ marginLeft: 8 }}>RAG 감시 중</span>}
        </div>

        <Toggle checked={showHidden} onChange={setShowHidden}>숨김 표시</Toggle>
        <button onClick={() => setTrashOpen(true)}>휴지통</button>
        <button onClick={() => { setText(''); setDialog({ kind: 'mkdir' }) }}>새 폴더</button>
        <button className="primary" onClick={() => fileRef.current?.click()} disabled={uploading}>
          {uploading ? '업로드 중…' : '업로드'}
        </button>
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={(e) => e.target.files && upload(e.target.files)}
        />
      </header>

      <div className={`drop${over ? ' over' : ''}`}>여기로 파일을 끌어다 놓으면 업로드됩니다</div>

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
              <tr>
                <td className="name">
                  <span className="ico" aria-hidden="true">↰</span>
                  <span className="fname" onClick={() => go(data.parent!)}>상위 폴더</span>
                </td>
                <td colSpan={3} />
              </tr>
            )}
            {data?.items.map((it) => (
              <tr key={it.path} className={it.hidden ? 'is-hidden' : ''}>
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
        <Modal title={`'${dialog.item.name}' 이동`} onClose={() => setDialog(null)} onSubmit={submitDialog} submitLabel="이동">
          <Field label="목적지 폴더" hint="NAS 루트 기준 상대경로. 비우면 루트">
            <input value={text} autoFocus placeholder="documents/2026" onChange={(e) => setText(e.target.value)} />
          </Field>
        </Modal>
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

      {trashOpen && <Trash onClose={() => { setTrashOpen(false); load() }} />}
    </div>
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
