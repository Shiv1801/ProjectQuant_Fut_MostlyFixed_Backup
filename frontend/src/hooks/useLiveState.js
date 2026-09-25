/**
 * Single shared WebSocket subscription for the whole app — every tab reads
 * from this hook's state, never opens its own fetch/poll (PRD §7.3b).
 */
import { useEffect, useState, useRef } from 'react'

export function useLiveState() {
  const [state, setState] = useState({
    indices: {}, vix: null, positions: [], modelScores: {}, signals: [], riskState: null, regime: null,
  })
  const wsRef = useRef(null)

  useEffect(() => {
    const ws = new WebSocket(`ws://${window.location.host}/ws/live`)
    wsRef.current = ws
    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data)
      setState((prev) => mergePayload(prev, payload))
    }
    return () => ws.close()
  }, [])

  return state
}

function mergePayload(prev, payload) {
  // Route incoming pub/sub messages into the right slice of state based on channel/type
  if (payload.model_id) {
    return { ...prev, modelScores: { ...prev.modelScores, [`${payload.index_id}:${payload.model_id}`]: payload } }
  }
  if (payload.direction !== undefined) {
    return { ...prev, signals: [payload, ...prev.signals].slice(0, 50) }
  }
  return prev
}
