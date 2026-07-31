import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { ToastProvider } from './ui'
// Pretendard 는 YDS 6.0 의 공통 서체다. 동적 서브셋을 번들해 오프라인에서도 뜨게 한다.
import 'pretendard/dist/web/variable/pretendardvariable-dynamic-subset.css'
import './styles.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <ToastProvider>
        <App />
      </ToastProvider>
    </BrowserRouter>
  </StrictMode>,
)
