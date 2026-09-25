// Tab 2 — live chart + confluence gate status + full live Quant Models breakdown (A-G, collapsible)
export default function ChartsSignals({ liveState }) {
  return (
    <div className="flex gap-3" style={{ display: 'flex' }}>
      <div style={{ flex: 3 }} className="bg-card border border-borderDefault rounded-card p-3 min-h-[400px]">
        {/* TODO: mount lightweight-charts candlestick, WS-driven incremental updates */}
        <div className="text-textMuted text-[11px]">1-min chart — index selector here</div>
      </div>
      <div style={{ flex: 1 }} className="bg-card border border-borderDefault rounded-card p-3">
        <div className="text-[13px] mb-2">Quant Models (A–G)</div>
        {Object.entries(liveState.modelScores).map(([key, score]) => (
          <div key={key} className="text-[11px] border-b border-borderSubtle py-1 flex justify-between">
            <span className="text-textMuted">{key}</span>
            <span>{score.score ?? '—'}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
