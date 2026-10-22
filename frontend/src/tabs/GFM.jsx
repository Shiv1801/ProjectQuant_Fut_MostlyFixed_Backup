import { useEffect, useMemo, useState } from 'react'

const MODEL_SPECS = [
  { id: 'zscore_price_deviation', label: 'Z-Score', maxVoteWeight: 0.8 },
  { id: 'order_flow_imbalance', label: 'Spot OFI', maxVoteWeight: 0.7 },
  { id: 'nifty_monthly_futures_order_flow_imbalance', label: 'NIFTY Futures OFI', maxVoteWeight: 0.7 },
  { id: 'put_call_ratio_oi', label: 'PCR', maxVoteWeight: 0.4 },
  { id: 'max_pain', label: 'Max Pain', maxVoteWeight: 0.3 },
  { id: 'oi_change_analysis', label: 'OI Change', maxVoteWeight: 0.35 },
]

const DEFAULT_SETTINGS = { minModels: 5, confidenceThreshold: 60 }
const SETTINGS_KEY = 'projectquant.gfm.direction.settings'

function loadSettings() {
  try {
    const stored = JSON.parse(window.localStorage.getItem(SETTINGS_KEY) || 'null')
    return {
      minModels: Number.isInteger(stored?.minModels) ? Math.min(6, Math.max(1, stored.minModels)) : DEFAULT_SETTINGS.minModels,
      confidenceThreshold: Number.isFinite(stored?.confidenceThreshold)
        ? Math.min(100, Math.max(0, stored.confidenceThreshold))
        : DEFAULT_SETTINGS.confidenceThreshold,
    }
  } catch {
    return DEFAULT_SETTINGS
  }
}

function getModelView(spec, models) {
  const model = models.find((item) => item.model_id === spec.id)
  const contribution = model?.contribution
  const vote = contribution?.score_vote
  const isUnavailable = !model
    || ['error', 'unavailable', 'stale', 'not_applicable'].includes(model.health)
    || ['ERROR', 'STALE', 'UNAVAILABLE', 'DISABLED'].includes(model.integration_status)
    || !contribution
    || ['unavailable', 'disabled', 'not_evaluated'].includes(contribution.state)

  if (isUnavailable) {
    return { ...spec, status: 'Unavailable', verdict: 'Unavailable', confidence: null, direction: 0, model }
  }

  if (vote && Number.isFinite(vote.direction) && Number.isFinite(vote.weight) && vote.weight > 0) {
    const confidence = Math.round(Math.min(1, vote.weight / spec.maxVoteWeight) * 100)
    const direction = Math.sign(vote.direction)
    const verdict = confidence < 25
      ? 'Neutral'
      : direction > 0
        ? (confidence >= 75 ? 'Strong Bullish' : 'Bullish')
        : (confidence >= 75 ? 'Very Bearish' : 'Bearish')
    return { ...spec, status: 'Available', verdict, confidence, direction, model }
  }

  // A valid score-contributor with no vote is a neutral/abstaining signal, not missing data.
  if (contribution.state === 'score_contributor' || contribution.state === 'contributing') {
    return { ...spec, status: 'Available', verdict: 'Neutral', confidence: 0, direction: 0, model }
  }

  return { ...spec, status: 'Unavailable', verdict: 'Unavailable', confidence: null, direction: 0, model }
}

function summarizeDirection(modelViews, settings) {
  const passing = modelViews.filter(
    (model) => model.status === 'Available'
      && model.direction !== 0
      && model.confidence >= settings.confidenceThreshold,
  )
  if (passing.length < settings.minModels) {
    return {
      verdict: 'Insufficient Models',
      confidence: null,
      detail: `${passing.length}/6 models pass; ${settings.minModels} required`,
      passing,
    }
  }

  const bullish = passing.filter((model) => model.direction > 0)
  const bearish = passing.filter((model) => model.direction < 0)
  if (bullish.length === bearish.length) {
    return { verdict: 'No Consensus', confidence: null, detail: 'Passing models are tied', passing }
  }

  const winners = bullish.length > bearish.length ? bullish : bearish
  const meanConfidence = Math.round(winners.reduce((sum, model) => sum + model.confidence, 0) / winners.length)
  const isBullish = bullish.length > bearish.length
  const verdict = isBullish
    ? (meanConfidence >= 75 ? 'Strong Bullish' : 'Bullish')
    : (meanConfidence >= 75 ? 'Very Bearish' : 'Bearish')

  return {
    verdict,
    confidence: meanConfidence,
    detail: `${winners.length}/${passing.length} passing models agree`,
    passing,
  }
}

function verdictClass(verdict) {
  if (verdict.includes('Bullish')) return 'text-emerald-400'
  if (verdict.includes('Bearish')) return 'text-rose-400'
  if (verdict === 'Neutral') return 'text-textMuted'
  return 'text-amber-300'
}

export default function GFM({ liveState }) {
  const [settings, setSettings] = useState(loadSettings)
  const [showSettings, setShowSettings] = useState(true)

  useEffect(() => {
    try { window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)) } catch { /* storage may be disabled */ }
  }, [settings])

  const modelViews = useMemo(
    () => MODEL_SPECS.map((spec) => getModelView(spec, liveState.modelHealth || [])),
    [liveState.modelHealth],
  )
  const summary = useMemo(() => summarizeDirection(modelViews, settings), [modelViews, settings])
  const availableCount = modelViews.filter((model) => model.status === 'Available').length

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-base font-semibold">GFM</h1>
          <p className="text-xs text-textMuted mt-1">Directional model consensus · six equal-weight models</p>
        </div>
        <button
          type="button"
          onClick={() => setShowSettings((value) => !value)}
          className="rounded-pill border border-borderDefault px-3 py-1.5 text-xs text-textMuted hover:text-white"
          aria-expanded={showSettings}
        >
          {showSettings ? 'Hide settings' : 'Direction settings'}
        </button>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,2fr)_minmax(260px,1fr)] gap-3 items-start">
        <div className="rounded-card border border-borderDefault bg-card p-3">
          <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
            <div>
              <h2 className="text-sm font-semibold">Direction</h2>
              <p className="text-[11px] text-textMuted mt-1">{availableCount}/6 models reporting · equal vote per model</p>
            </div>
            <div className="text-right">
              <div className={`text-sm font-semibold ${verdictClass(summary.verdict)}`}>{summary.verdict}</div>
              <div className="text-[11px] text-textMuted">
                {summary.confidence == null ? 'Confidence —' : `Confidence ${summary.confidence}%`}
              </div>
              <div className="text-[10px] text-textMuted">{summary.detail}</div>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-borderDefault text-textMuted">
                  <th className="py-2 pr-3 font-medium">Model</th>
                  <th className="py-2 pr-3 font-medium">Direction</th>
                  <th className="py-2 pr-3 font-medium">Confidence</th>
                  <th className="py-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {modelViews.map((model) => (
                  <tr key={model.id} className="border-b border-borderSubtle last:border-0">
                    <td className="py-3 pr-3 font-medium">{model.label}</td>
                    <td className={`py-3 pr-3 font-medium ${verdictClass(model.verdict)}`}>{model.verdict}</td>
                    <td className="py-3 pr-3 tabular-nums">{model.confidence == null ? '—' : `${model.confidence}%`}</td>
                    <td className="py-3 text-textMuted">{model.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[10px] leading-4 text-textMuted mt-3">
            Confidence is normalized from each model's existing directional vote weight. Neutral, stale, or unavailable signals do not count as passing directional votes.
          </p>
        </div>

        {showSettings && (
          <aside className="rounded-card border border-borderDefault bg-card p-3 space-y-4">
            <div>
              <h2 className="text-sm font-semibold">Direction Settings</h2>
              <p className="text-[11px] text-textMuted mt-1">Saved in this browser; does not change trading logic.</p>
            </div>
            <div>
              <div className="flex items-center justify-between gap-2">
                <label htmlFor="gfm-min-models" className="text-xs">Minimum models to pass</label>
                <span className="text-xs font-semibold tabular-nums">{settings.minModels}/6</span>
              </div>
              <input
                id="gfm-min-models"
                type="range"
                min="1"
                max="6"
                step="1"
                value={settings.minModels}
                onChange={(event) => setSettings((current) => ({ ...current, minModels: Number(event.target.value) }))}
                className="w-full mt-2 accent-sky-400"
              />
              <div className="flex justify-between text-[10px] text-textMuted"><span>1 model</span><span>6 models</span></div>
            </div>
            <div>
              <div className="flex items-center justify-between gap-2">
                <label htmlFor="gfm-confidence" className="text-xs">Minimum confidence</label>
                <span className="text-xs font-semibold tabular-nums">{settings.confidenceThreshold}%</span>
              </div>
              <input
                id="gfm-confidence"
                type="range"
                min="0"
                max="100"
                step="5"
                value={settings.confidenceThreshold}
                onChange={(event) => setSettings((current) => ({ ...current, confidenceThreshold: Number(event.target.value) }))}
                className="w-full mt-2 accent-sky-400"
              />
              <div className="flex justify-between text-[10px] text-textMuted"><span>0%</span><span>100%</span></div>
            </div>
            <div className="rounded-md border border-borderSubtle p-2 text-[11px] text-textMuted leading-4">
              A combined verdict is shown only when the minimum number of valid models meet the confidence threshold. Ties show No Consensus.
            </div>
            <button
              type="button"
              onClick={() => setSettings(DEFAULT_SETTINGS)}
              className="text-xs text-textMuted underline underline-offset-2 hover:text-white"
            >
              Reset settings
            </button>
          </aside>
        )}
      </div>
    </section>
  )
}
