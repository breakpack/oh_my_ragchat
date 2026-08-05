import { useCallback, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { api } from './api'
import Auth from './pages/Auth'
import Chat from './pages/Chat'
import Files from './pages/Files'
import Knowledge from './pages/Knowledge'
import Jobs from './pages/Jobs'
import Notion from './pages/Notion'
import Settings from './pages/Settings'

interface Me {
  configured: boolean
  authenticated: boolean
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null)

  const refresh = useCallback(async () => {
    try {
      setMe(await api.get<Me>('/api/auth/me'))
    } catch {
      setMe({ configured: true, authenticated: false })
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  if (!me) {
    return (
      <div className="auth">
        <div className="muted">불러오는 중…</div>
      </div>
    )
  }

  if (!me.authenticated) return <Auth configured={me.configured} onDone={refresh} />

  return (
    <div className="app">
      <nav className="nav" aria-label="주요 메뉴">
        <div className="brand">cc</div>
        {/* YDS 는 검증되지 않은 icon glyph 도입을 금지한다(DESIGN.md §7). 텍스트 label 로 둔다. */}
        {[
          { to: '/chat', label: '채팅' },
          { to: '/files', label: '파일' },
          { to: '/knowledge', label: '지식' },
          { to: '/notion', label: 'Notion' },
          { to: '/jobs', label: '작업' },
          { to: '/settings', label: '설정' },
        ].map((m) => (
          <NavLink key={m.to} to={m.to} className={({ isActive }) => (isActive ? 'active' : '')}>
            {m.label}
          </NavLink>
        ))}
        <div style={{ flex: 1 }} />
        <button
          className="ghost sm"
          aria-label="로그아웃"
          onClick={async () => {
            await api.post('/api/auth/logout')
            refresh()
          }}
        >
          로그아웃
        </button>
      </nav>

      <div className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<Chat />} />
          <Route path="/chat/:sessionId" element={<Chat />} />
          <Route path="/files" element={<Files />} />
          <Route path="/knowledge" element={<Knowledge />} />
          <Route path="/notion" element={<Notion />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>
      </div>
    </div>
  )
}
