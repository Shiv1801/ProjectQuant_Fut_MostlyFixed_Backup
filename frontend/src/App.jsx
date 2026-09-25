import { useState } from 'react'
import Dashboard from './tabs/Dashboard.jsx'
import ChartsSignals from './tabs/ChartsSignals.jsx'
import OptionChain from './tabs/OptionChain.jsx'
import PositionsRisk from './tabs/PositionsRisk.jsx'
import TradeLog from './tabs/TradeLog.jsx'
import BacktestModels from './tabs/BacktestModels.jsx'
import TopBar from './components/TopBar.jsx'
import { useLiveState } from './hooks/useLiveState.js'

const TABS = {
  dashboard: Dashboard,
  charts: ChartsSignals,
  chain: OptionChain,
  positions: PositionsRisk,
  tradelog: TradeLog,
  backtest: BacktestModels,
}

export default function App() {
  const [activeTab, setActiveTab] = useState('dashboard')
  // Single WS connection, shared across all tabs — never per-tab fetching (PRD §7.3b)
  const liveState = useLiveState()
  const ActiveTab = TABS[activeTab]

  return (
    <div className="min-h-screen bg-bg">
      <TopBar activeTab={activeTab} setActiveTab={setActiveTab} regime={liveState.regime} />
      <main className="p-3">
        <ActiveTab liveState={liveState} />
      </main>
    </div>
  )
}
