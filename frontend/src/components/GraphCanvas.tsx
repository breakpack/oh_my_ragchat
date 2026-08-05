import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

export interface GNode {
  id: number
  name: string
  type: string
  description: string
  degree: number
  hop?: number
}

export interface GEdge {
  id: number
  source: number
  target: number
  description?: string
  weight?: number
}

interface Body {
  x: number
  y: number
  vx: number
  vy: number
  r: number
  fixed?: boolean
}

/* ── 힘 계수. 옵시디언식 배치를 목표로 맞춘 값 ── */
const REPULSION = 9000 // 노드끼리 밀어내는 힘
const LINK_LEN = 90 // 링크가 유지하려는 길이
const LINK_K = 0.03 // 링크 스프링 강도
const CENTER_K = 0.008 // 중심으로 당기는 힘 (고립 노드가 날아가지 않게)
const COLLIDE_PAD = 4 // 노드가 서로 겹치지 않게 두는 여백
const DAMPING = 0.86
const ALPHA_DECAY = 0.985
const ALPHA_MIN = 0.004
const LABEL_BASE = 24 // 기본 배율에서 라벨을 보여줄 상위 노드 수

// 연결 수 차이가 한눈에 보이도록 반지름 폭을 넓게 잡는다
const radiusOf = (degree: number) => 4 + Math.min(Math.sqrt(degree || 0) * 2.2, 22)

export default function GraphCanvas({
  nodes,
  edges,
  focusId,
  showLabels = true,
  onOpen,
  onHover,
}: {
  nodes: GNode[]
  edges: GEdge[]
  focusId?: number | null
  showLabels?: boolean
  onOpen?: (node: GNode) => void
  onHover?: (node: GNode | null) => void
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const bodies = useRef(new Map<number, Body>())
  const alpha = useRef(1)
  const raf = useRef(0)
  const view = useRef({ k: 1, tx: 0, ty: 0 })
  const drag = useRef<{ id?: number; sx: number; sy: number; ox: number; oy: number } | null>(null)
  const [hover, setHover] = useState<number | null>(null)
  const hoverRef = useRef<number | null>(null)
  const [size, setSize] = useState({ w: 800, h: 520 })

  // 이웃 색인 — 하이라이트할 때 쓴다
  const neighbors = useMemo(() => {
    const m = new Map<number, Set<number>>()
    for (const e of edges) {
      if (!m.has(e.source)) m.set(e.source, new Set())
      if (!m.has(e.target)) m.set(e.target, new Set())
      m.get(e.source)!.add(e.target)
      m.get(e.target)!.add(e.source)
    }
    return m
  }, [edges])

  // 연결이 많은 순서 — 라벨을 보여줄 우선순위로 쓴다
  const ranked = useMemo(
    () => [...nodes].sort((a, b) => (b.degree || 0) - (a.degree || 0)),
    [nodes],
  )

  const autoFit = useRef(true)

  /** 전체가 화면에 들어오도록 배율·이동을 맞춘다. */
  const fitView = useCallback(() => {
    const list = Array.from(bodies.current.values())
    if (!list.length) return
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
    for (const b of list) {
      minX = Math.min(minX, b.x - b.r)
      minY = Math.min(minY, b.y - b.r)
      maxX = Math.max(maxX, b.x + b.r)
      maxY = Math.max(maxY, b.y + b.r)
    }
    const pad = 48
    const w = Math.max(maxX - minX, 1)
    const h = Math.max(maxY - minY, 1)
    const k = Math.max(0.25, Math.min((size.w - pad * 2) / w, (size.h - pad * 2) / h, 1.6))
    view.current = {
      k,
      tx: size.w / 2 - ((minX + maxX) / 2) * k,
      ty: size.h / 2 - ((minY + maxY) / 2) * k,
    }
  }, [size])

  /* ── 노드 집합이 바뀌면 위치 초기화 ── */
  useEffect(() => {
    const next = new Map<number, Body>()
    const { w, h } = size
    nodes.forEach((n, i) => {
      const prev = bodies.current.get(n.id)
      if (prev) {
        next.set(n.id, { ...prev, r: radiusOf(n.degree) })
        return
      }
      // 황금각 나선으로 흩뿌려 초기 겹침을 줄인다
      const t = i * 2.399963
      const rad = 18 * Math.sqrt(i + 1)
      next.set(n.id, {
        x: w / 2 + Math.cos(t) * rad,
        y: h / 2 + Math.sin(t) * rad,
        vx: 0,
        vy: 0,
        r: radiusOf(n.degree),
      })
    })
    bodies.current = next
    alpha.current = 1
    autoFit.current = true
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes])

  /* ── 컨테이너 크기 추적 ── */
  useEffect(() => {
    const el = canvasRef.current?.parentElement
    if (!el) return
    const ro = new ResizeObserver(() => {
      setSize({ w: el.clientWidth, h: el.clientHeight })
    })
    ro.observe(el)
    setSize({ w: el.clientWidth, h: el.clientHeight })
    return () => ro.disconnect()
  }, [])

  /* ── 시뮬레이션 한 스텝 ── */
  const step = useCallback(() => {
    const list = Array.from(bodies.current.values())
    const ids = Array.from(bodies.current.keys())
    const a = alpha.current
    const { w, h } = size

    // 반발력 (O(n²) — 노드 수백 개까지는 충분히 빠르다)
    for (let i = 0; i < list.length; i++) {
      const A = list[i]
      for (let j = i + 1; j < list.length; j++) {
        const B = list[j]
        let dx = B.x - A.x
        let dy = B.y - A.y
        let d2 = dx * dx + dy * dy
        if (d2 < 1) {
          dx = (Math.random() - 0.5) * 2
          dy = (Math.random() - 0.5) * 2
          d2 = 4
        }
        const d = Math.sqrt(d2)
        let f = (REPULSION * a) / d2
        // 반지름이 겹치면 강하게 밀어낸다 (충돌 회피)
        const minD = A.r + B.r + COLLIDE_PAD
        if (d < minD) f += (minD - d) * 0.6
        const fx = (dx / d) * f
        const fy = (dy / d) * f
        A.vx -= fx
        A.vy -= fy
        B.vx += fx
        B.vy += fy
      }
    }

    // 링크 스프링
    for (const e of edges) {
      const A = bodies.current.get(e.source)
      const B = bodies.current.get(e.target)
      if (!A || !B) continue
      const dx = B.x - A.x
      const dy = B.y - A.y
      const d = Math.hypot(dx, dy) || 1
      const f = (d - LINK_LEN) * LINK_K * a
      const fx = (dx / d) * f
      const fy = (dy / d) * f
      A.vx += fx
      A.vy += fy
      B.vx -= fx
      B.vy -= fy
    }

    // 중심 인력 + 적분
    for (let i = 0; i < list.length; i++) {
      const b = list[i]
      if (b.fixed) {
        b.vx = 0
        b.vy = 0
        continue
      }
      b.vx += (w / 2 - b.x) * CENTER_K * a
      b.vy += (h / 2 - b.y) * CENTER_K * a
      b.vx *= DAMPING
      b.vy *= DAMPING
      b.x += b.vx
      b.y += b.vy
    }
    void ids
    alpha.current = a * ALPHA_DECAY
  }, [edges, size])

  /* ── 렌더링 ── */
  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const dpr = window.devicePixelRatio || 1
    const { w, h } = size
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr
      canvas.height = h * dpr
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, w, h)

    const { k, tx, ty } = view.current
    ctx.translate(tx, ty)
    ctx.scale(k, k)

    const hi = hoverRef.current
    const near = hi !== null ? neighbors.get(hi) : undefined
    const dimmed = hi !== null

    // 링크
    ctx.lineCap = 'round'
    for (const e of edges) {
      const A = bodies.current.get(e.source)
      const B = bodies.current.get(e.target)
      if (!A || !B) continue
      const active = hi !== null && (e.source === hi || e.target === hi)
      ctx.strokeStyle = active ? 'rgba(29,139,255,.55)' : dimmed ? 'rgba(230,230,230,.5)' : '#e6e6e6'
      ctx.lineWidth = active ? 1.6 : 1
      ctx.beginPath()
      ctx.moveTo(A.x, A.y)
      ctx.lineTo(B.x, B.y)
      ctx.stroke()
    }

    // 노드
    for (const n of nodes) {
      const b = bodies.current.get(n.id)
      if (!b) continue
      const isHover = n.id === hi
      const isNear = near?.has(n.id)
      const isFocus = focusId != null && n.id === focusId
      let fill = '#49627a'
      if (isFocus || isHover) fill = '#1d8bff'
      else if (isNear) fill = '#5b8fd6'
      else if (dimmed) fill = '#c9d1da'

      ctx.beginPath()
      ctx.arc(b.x, b.y, b.r, 0, Math.PI * 2)
      ctx.fillStyle = fill
      ctx.fill()
      if (isHover || isFocus) {
        ctx.lineWidth = 2 / k
        ctx.strokeStyle = '#ffffff'
        ctx.stroke()
      }
    }

    // 라벨 — 연결이 많은 순으로 보여주되, 이미 그린 라벨과 겹치면 건너뛴다.
    // 확대할수록 더 많이 나온다 (옵시디언과 같은 방식).
    if (showLabels) {
      ctx.textAlign = 'center'
      ctx.textBaseline = 'top'
      const budget = Math.round(LABEL_BASE * Math.max(1, k * k))
      const boxes: [number, number, number, number][] = []
      const fontPx = 12 / k

      let drawn = 0
      for (const n of ranked) {
        const b = bodies.current.get(n.id)
        if (!b) continue
        const isHover = n.id === hi
        const isNear = near?.has(n.id)
        const isFocus = focusId != null && n.id === focusId
        const always = isHover || isFocus || isNear
        if (!always && drawn >= budget) continue

        ctx.font = `600 ${fontPx}px -apple-system, 'Pretendard Variable', Pretendard, sans-serif`
        const label = n.name.length > 20 ? `${n.name.slice(0, 19)}…` : n.name
        const tw = ctx.measureText(label).width
        const x0 = b.x - tw / 2
        const y0 = b.y + b.r + 3 / k
        const box: [number, number, number, number] = [x0, y0, tw, fontPx * 1.15]

        if (!always) {
          const clash = boxes.some(
            ([bx, by, bw, bh]) =>
              x0 < bx + bw && x0 + box[2] > bx && y0 < by + bh && y0 + box[3] > by,
          )
          if (clash) continue
        }
        boxes.push(box)
        if (!always) drawn++

        ctx.lineWidth = 3 / k
        ctx.strokeStyle = 'rgba(255,255,255,.95)'
        ctx.strokeText(label, b.x, y0)
        ctx.fillStyle = dimmed && !always ? '#b6bec7' : '#222222'
        ctx.fillText(label, b.x, y0)
      }
    }
  }, [edges, nodes, ranked, neighbors, size, focusId, showLabels])

  /* ── 애니메이션 루프 ── */
  useEffect(() => {
    const loop = () => {
      if (alpha.current > ALPHA_MIN) {
        step()
        // 자리를 잡아가는 동안에는 계속 화면에 맞춘다 (사용자가 만지면 멈춘다)
        if (autoFit.current) fitView()
      }
      draw()
      raf.current = requestAnimationFrame(loop)
    }
    raf.current = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf.current)
  }, [step, draw, fitView])

  /* ── 좌표 변환 & 히트 테스트 ── */
  const toWorld = (cx: number, cy: number) => {
    const { k, tx, ty } = view.current
    return { x: (cx - tx) / k, y: (cy - ty) / k }
  }

  const hit = (cx: number, cy: number): GNode | null => {
    const { x, y } = toWorld(cx, cy)
    let best: GNode | null = null
    let bestD = Infinity
    for (const n of nodes) {
      const b = bodies.current.get(n.id)
      if (!b) continue
      const d = Math.hypot(b.x - x, b.y - y)
      if (d < b.r + 6 && d < bestD) {
        best = n
        bestD = d
      }
    }
    return best
  }

  const localPos = (e: { clientX: number; clientY: number }) => {
    const rect = canvasRef.current!.getBoundingClientRect()
    return { cx: e.clientX - rect.left, cy: e.clientY - rect.top }
  }

  return (
    <div className="graph-canvas">
      <canvas
        ref={canvasRef}
        style={{ width: '100%', height: '100%', display: 'block', cursor: hover ? 'pointer' : 'grab' }}
        onPointerDown={(e) => {
          const { cx, cy } = localPos(e)
          const node = hit(cx, cy)
          canvasRef.current?.setPointerCapture(e.pointerId)
          autoFit.current = false
          if (node) {
            const b = bodies.current.get(node.id)!
            b.fixed = true
            drag.current = { id: node.id, sx: cx, sy: cy, ox: b.x, oy: b.y }
          } else {
            drag.current = { sx: cx, sy: cy, ox: view.current.tx, oy: view.current.ty }
          }
        }}
        onPointerMove={(e) => {
          const { cx, cy } = localPos(e)
          const d = drag.current
          if (d) {
            if (d.id != null) {
              const { k } = view.current
              const b = bodies.current.get(d.id)!
              b.x = d.ox + (cx - d.sx) / k
              b.y = d.oy + (cy - d.sy) / k
              alpha.current = Math.max(alpha.current, 0.28)
            } else {
              view.current.tx = d.ox + (cx - d.sx)
              view.current.ty = d.oy + (cy - d.sy)
            }
            return
          }
          const node = hit(cx, cy)
          const id = node?.id ?? null
          if (id !== hoverRef.current) {
            hoverRef.current = id
            setHover(id)
            onHover?.(node ?? null)
          }
        }}
        onPointerUp={() => {
          const d = drag.current
          if (d?.id != null) {
            const b = bodies.current.get(d.id)
            if (b) b.fixed = false
          }
          drag.current = null
        }}
        onPointerLeave={() => {
          drag.current = null
          hoverRef.current = null
          setHover(null)
          onHover?.(null)
        }}
        onDoubleClick={(e) => {
          const { cx, cy } = localPos(e)
          const node = hit(cx, cy)
          if (node && onOpen) onOpen(node)
        }}
        onWheel={(e) => {
          const { cx, cy } = localPos(e)
          autoFit.current = false
          const v = view.current
          const factor = Math.exp(-e.deltaY * 0.0015)
          const k = Math.max(0.25, Math.min(v.k * factor, 5))
          // 커서 아래 지점이 고정되도록 이동량 보정
          v.tx = cx - ((cx - v.tx) / v.k) * k
          v.ty = cy - ((cy - v.ty) / v.k) * k
          v.k = k
        }}
      />

      <div className="graph-actions">
        <button
          className="sm"
          onClick={() => {
            alpha.current = 1
            autoFit.current = true
          }}
        >
          재배치
        </button>
      </div>
    </div>
  )
}
