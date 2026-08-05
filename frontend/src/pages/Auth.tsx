import { useState } from 'react'
import { api } from '../api'
import { Field, useRun } from '../ui'

export default function Auth({ configured, onDone }: { configured: boolean; onDone: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const run = useRun()

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!configured && password !== confirm) {
      alert('비밀번호가 일치하지 않습니다')
      return
    }
    setBusy(true)
    const ok = await run(() =>
      configured
        ? api.post('/api/auth/login', { username, password })
        : api.post('/api/auth/setup', { username, password }),
    )
    setBusy(false)
    if (ok) onDone()
  }

  return (
    <div className="auth">
      <form className="card" onSubmit={submit}>
        <h1>chatchat</h1>
        <p className="mute2" style={{ marginTop: 0 }}>
          {configured
            ? '아이디와 비밀번호를 입력하세요.'
            : '처음 실행입니다. 관리자 계정을 만드세요. 이후 계정은 설정에서 추가합니다.'}
        </p>

        <Field label="아이디">
          <input
            value={username}
            autoFocus
            autoComplete="username"
            onChange={(e) => setUsername(e.target.value)}
          />
        </Field>

        <Field label="비밀번호">
          <input
            type="password"
            value={password}
            autoComplete={configured ? 'current-password' : 'new-password'}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>

        {!configured && (
          <Field label="비밀번호 확인">
            <input
              type="password"
              value={confirm}
              autoComplete="new-password"
              onChange={(e) => setConfirm(e.target.value)}
            />
          </Field>
        )}

        <button className="primary" style={{ width: '100%' }} disabled={busy || !username || !password}>
          {busy ? '처리 중…' : configured ? '들어가기' : '관리자 계정 만들기'}
        </button>
      </form>
    </div>
  )
}
