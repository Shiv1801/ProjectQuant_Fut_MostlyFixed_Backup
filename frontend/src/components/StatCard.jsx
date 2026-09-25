export default function StatCard({ label, value, tone = 'neutral' }) {
  const toneClass = { positive: 'text-positive', negative: 'text-negative', neutral: 'text-textPrimary' }[tone]
  return (
    <div className="bg-cardInset border border-borderDefault rounded-card p-3">
      <div className="text-[10px] text-textMuted uppercase">{label}</div>
      <div className={`text-[20px] ${toneClass}`}>{value}</div>
    </div>
  )
}
