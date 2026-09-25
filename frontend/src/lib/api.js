const BASE = '/api'

export async function fetchTab(path) {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json()
}

export async function switchMode(target_mode) {
  return fetch(`${BASE}/mode/switch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target_mode, confirm: true }),
  }).then((r) => r.json())
}

export async function killSwitch() {
  return fetch(`${BASE}/mode/kill-switch`, { method: 'POST' }).then((r) => r.json())
}
