import { useState } from 'react'
import { api } from '../api'
import { Field, useRun } from '../ui'

export default function Auth({ configured, onDone }: { configured: boolean; onDone: () => void }) {
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
      configured ? api.post('/api/auth/login', { password }) : api.post('/api/auth/setup', { password }),
    )
    setBusy(false)
    if (ok) onDone()
  }

  return (
    <div className="auth">
      <form className="card" onSubmit={submit}>
        <h1>chatchat</h1>
        <p className="mute2" style={{ marginTop: 0 }}>
          {configured ? '비밀번호를 입력해 잠금을 해제하세요.' : '처음 실행입니다. 사용할 비밀번호를 정하세요.'}
        </p>

        <Field label="비밀번호">
          <input
            type="password"
            value={password}
            autoFocus
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

        <button className="primary" style={{ width: '100%' }} disabled={busy || !password}>
          {busy ? '처리 중…' : configured ? '들어가기' : '시작하기'}
        </button>
      </form>
    </div>
  )
}
