/** Persistent regime indicator across all tabs (PRD §9.2 UI suggestion) + tab nav + mode badge. */
export default function TopBar({ activeTab, setActiveTab, regime }) {
  const tabs = [
    ['dashboard', 'Dashboard'], ['charts', 'Charts & Signals'], ['chain', 'Option Chain'],
    ['positions', 'Positions & Risk'], ['tradelog', 'Trade Log'], ['backtest', 'Backtest & Models'],
  ]
  return (
    <header className="border-b border-borderDefault bg-card">
      {/* Regime strip — always visible, glanceable, colored by regime */}
      <div className="h-1 w-full" style={{ backgroundColor: regime === 'trending' ? '#4ade80' : regime === 'mean_reverting' ? '#60a5fa' : '#6b7688' }} />
      <nav className="flex gap-4 px-3 py-2 text-[11px]">
        {tabs.map(([key, label]) => (
          <button
            key={key}
            onClick={() => setActiveTab(key)}
            className={`px-2 py-1 rounded-pill ${activeTab === key ? 'text-accent border-b border-accent' : 'text-textMuted'}`}
          >
            {label}
          </button>
        ))}
      </nav>
    </header>
  )
}
