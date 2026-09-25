// Tab 5 — every closed trade, filterable/searchable, exit_reason enum tags
export default function TradeLog({ liveState }) {
  return (
    <div className="bg-card border border-borderDefault rounded-card p-3">
      <input className="bg-cardInset border border-borderDefault rounded-card px-2 py-1 text-[11px] mb-2" placeholder="Search trades..." />
      <table className="w-full text-[11px]">
        <thead className="text-textMuted">
          <tr><th>Date</th><th>Symbol</th><th>Entry</th><th>Exit</th><th>PnL ₹</th><th>PnL %</th><th>Reason</th></tr>
        </thead>
        <tbody>{/* TODO: fetch /api/trade-log and map rows */}</tbody>
      </table>
    </div>
  )
}
