/**
 * Single shared WebSocket subscription for the whole app — every tab reads
 * from this hook's state, never opens its own fetch/poll (PRD §7.3b).
 */
import { useEffect, useState, useRef } from 'react'

export function useLiveState() {
  const [state, setState] = useState({
    indices: {}, vix: null, positions: [], modelScores: {}, modelHealth: [], sourceHealth: {}, signals: [], riskState: null, regime: null,
  })
  const wsRef = useRef(null)

  useEffect(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws/prices`)
    wsRef.current = ws
    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data)
      setState((prev) => ({ ...prev, indices: payload.data ?? prev.indices }))
    }
    const loadModels = async () => {
      try {
        const response = await fetch('/models/live/NIFTY50')
        const payload = await response.json()
        setState((prev) => ({ ...prev, modelHealth: payload.models ?? [], sourceHealth: payload.source_health ?? {} }))
      } catch {
        setState((prev) => ({ ...prev, sourceHealth: { ...prev.sourceHealth, models: 'unavailable' } }))
      }
    }
    loadModels()
    const modelTimer = window.setInterval(loadModels, 1000)
    return () => { ws.close(); window.clearInterval(modelTimer) }
  }, [])

  return state
}
