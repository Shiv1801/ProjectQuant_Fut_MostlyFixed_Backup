// Tab 1 — at-a-glance system state: 3 indices, VIX, open positions, today's P&L, mode indicator
import StatCard from '../components/StatCard.jsx'
import Badge from '../components/Badge.jsx'

export default function Dashboard({ liveState }) {
  return (
    <div className="grid grid-cols-4 gap-3">
      <StatCard label="Today's P&L" value="₹0" tone="neutral" />
      <StatCard label="Open Positions" value={liveState.positions.length} />
      <StatCard label="India VIX" value={liveState.vix ?? '—'} />
      <div className="flex items-center gap-2"><Badge label="PAPER" kind="paper" /></div>
    </div>
  )
}
