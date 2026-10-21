import { useEffect, useRef, useState } from 'react'
import { createChart } from 'lightweight-charts'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export default function ChartsSignals({ liveState }) {
  const chartRef = useRef(null)
  const containerRef = useRef(null)
  const [candles, setCandles] = useState([])
  const [futures, setFutures] = useState(null)
  const [status, setStatus] = useState('Loading futures candles...')

  useEffect(() => {
    let active = true
    const load = async () => {
      try {
        const [candleResponse, futuresResponse] = await Promise.all([
          fetch(`${API_BASE}/candles/NIFTY50?limit=500`),
          fetch(`${API_BASE}/market/futures`),
        ])
        const payload = await candleResponse.json()
        const futuresPayload = await futuresResponse.json()
        if (!active) return
        setCandles(payload.candles ?? [])
        setFutures(futuresPayload)
        setStatus(freshnessStatus(futuresPayload, payload.count))
      } catch {
        if (active) setStatus('Futures candle API unavailable')
      }
    }
    load()
    const timer = window.setInterval(load, 1_000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  useEffect(() => {
    if (!containerRef.current) return undefined
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: { background: { color: 'transparent' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
      timeScale: {
        timeVisible: true, secondsVisible: true, borderColor: '#334155',
        tickMarkFormatter: istTime,
      },
      localization: { timeFormatter: istTime },
      rightPriceScale: { borderColor: '#334155' },
    })
    const series = chart.addCandlestickSeries({
      upColor: '#22c55e', downColor: '#ef4444', borderVisible: false,
      wickUpColor: '#22c55e', wickDownColor: '#ef4444',
    })
    const data = mergeLiveFutureQuote(candles, futures).map((candle) => ({
      time: Math.floor(Date.parse(candle.candle_timestamp) / 1000),
      open: candle.open, high: candle.high, low: candle.low, close: candle.close,
    })).filter((candle) => Number.isFinite(candle.time))
    series.setData(data)
    if (data.length) chart.timeScale().fitContent()
    chartRef.current = chart
    const resizeObserver = new ResizeObserver(() => chart.applyOptions({ width: containerRef.current.clientWidth }))
    resizeObserver.observe(containerRef.current)
    return () => { resizeObserver.disconnect(); chart.remove(); chartRef.current = null }
  }, [candles, futures])

  const displayCandles = mergeLiveFutureQuote(candles, futures)
  const latest = displayCandles[displayCandles.length - 1]
  return (
    <div className="flex gap-3" style={{ display: 'flex' }}>
      <div style={{ flex: 3 }} className="bg-card border border-borderDefault rounded-card p-3 min-h-[400px]">
        <div className="flex justify-between text-[11px] mb-2">
          <span className="text-textMuted">NIFTY futures · continuous 1-min candles</span>
          <span className="text-textMuted">{status} · {formatIst(futures?.quotes?.current_month?.fetched_at)}</span>
        </div>
        <div ref={containerRef} style={{ height: 360, width: '100%' }} />
        {latest && <div className="text-textMuted text-[10px] mt-1">Source contract: {latest.contract_symbol}</div>}
      </div>
      <div style={{ flex: 1 }} className="bg-card border border-borderDefault rounded-card p-3">
        <div className="text-[13px] mb-2">Quant Models (A–G)</div>
        {liveState.modelHealth.map((model) => (
          <div key={model.model_id} className="text-[11px] border-b border-borderSubtle py-1">
            <div className="flex justify-between">
              <span className="text-textMuted">{model.model_id}</span>
              <span className={healthColor(model.integration_status)}>{model.integration_status}</span>
            </div>
            <div className="text-textMuted text-[10px]">{modelValue(model)}</div>
            <div className="text-textMuted text-[10px]">Health: {model.health} · Eval: {formatIst(model.evaluation_id || model.computed_at)}</div>
            {model.integration_reason && <div className="text-textMuted text-[10px]">{model.integration_reason}</div>}
          </div>
        ))}
      </div>
    </div>
  )
}

function istTime(timestamp) {
  return new Intl.DateTimeFormat('en-IN', {
    timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(new Date(timestamp * 1000))
}

function modelValue(model) {
  const result = model.result ?? {}
  return result.direction ?? result.confidence ?? result.score ?? (result.active !== undefined ? `active=${result.active}` : model.note ?? '—')
}

function healthColor(status) {
  if (status === 'ACTIVE & CONTRIBUTING') return 'text-emerald-400'
  if (status === 'ACTIVE — NOT CONTRIBUTING' || status === 'DIAGNOSTIC ONLY' || status === 'DISABLED') return 'text-amber-400'
  return 'text-red-400'
}

function mergeLiveFutureQuote(candles, futures) {
  const quote = futures?.quotes?.current_month
  if (!quote?.ltp || !candles.length) return candles
  const latest = candles[candles.length - 1]
  const quoteTime = Date.parse(quote.fetched_at)
  const candleTime = Date.parse(latest.candle_timestamp)
  if (!Number.isFinite(quoteTime) || !Number.isFinite(candleTime) || Math.floor(quoteTime / 60000) !== Math.floor(candleTime / 60000)) return candles
  const adjustment = Number(latest.continuous_adjustment ?? 0)
  const ltp = Number(quote.ltp) + adjustment
  const high = Math.max(Number(latest.high), ltp)
  const low = Math.min(Number(latest.low), ltp)
  return [...candles.slice(0, -1), { ...latest, close: ltp, high, low }]
}

function freshnessStatus(futures, candleCount) {
  const quote = futures?.quotes?.current_month
  if (!quote) return candleCount ? 'STALE · no live futures quote' : 'DATA ERROR · no futures data'
  const age = (Date.now() - Date.parse(quote.fetched_at)) / 1000
  if (quote.status === 'valid' && age <= 8) return 'LIVE'
  if (futures?.market_status === 'market_closed') return 'MARKET CLOSED'
  if (quote.error) return 'DATA ERROR · ' + quote.error
  return 'STALE'
}

function formatIst(value) {
  if (!value) return '—'
  const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`
  const date = new Date(normalized)
  if (Number.isNaN(date.getTime())) return '—'
  return `${new Intl.DateTimeFormat('en-IN', {
    timeZone: 'Asia/Kolkata', dateStyle: 'short', timeStyle: 'medium', hour12: false,
  }).format(date)} IST`
}
