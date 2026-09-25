// Tab 6 — model scorecards, backtest runner, ML version history, champion/challenger status
export default function BacktestModels({ liveState }) {
  return (
    <div className="grid grid-cols-2 gap-3">
      <div className="bg-card border border-borderDefault rounded-card p-3">
        <div className="text-[13px] mb-2">Model Scorecards</div>
        {/* TODO: fetch /api/backtest-models */}
      </div>
      <div className="bg-card border border-borderDefault rounded-card p-3">
        <div className="text-[13px] mb-2">Champion / Challenger</div>
      </div>
    </div>
  )
}
