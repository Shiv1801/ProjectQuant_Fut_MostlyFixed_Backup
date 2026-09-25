// Tab 3 — full chain view with Greeks, IV rank, OI
export default function OptionChain({ liveState }) {
  return (
    <div className="bg-card border border-borderDefault rounded-card p-3">
      <table className="w-full text-[11px]">
        <thead className="text-textMuted">
          <tr><th>Strike</th><th>CE LTP</th><th>CE Delta</th><th>IV</th><th>OI</th><th>PE LTP</th><th>PE Delta</th></tr>
        </thead>
        <tbody>{/* TODO: map option_chain rows from liveState */}</tbody>
      </table>
    </div>
  )
}
