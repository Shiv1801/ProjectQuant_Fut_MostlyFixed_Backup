// Tab 4 — open/closed positions, live SL/TSL state, circuit breaker status
import Badge from '../components/Badge.jsx'

export default function PositionsRisk({ liveState }) {
  return (
    <div className="bg-card border border-borderDefault rounded-card p-3">
      <div className="text-[13px] mb-2">Open Positions</div>
      {liveState.positions.map((p) => (
        <div key={p.id} className="flex justify-between text-[11px] border-b border-borderSubtle py-1">
          <span>{p.symbol}</span>
          <Badge label={p.mode?.toUpperCase() ?? 'PAPER'} kind={p.mode === 'live' ? 'live' : 'paper'} />
        </div>
      ))}
    </div>
  )
}
