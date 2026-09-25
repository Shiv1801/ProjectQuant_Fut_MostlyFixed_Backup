/** LIVE/PAPER/exit-reason pill — exact tokens per PRD §7.5. */
const COLORS = {
  live: '#4ade80', paper: '#60a5fa', warning: '#fbbf24', loss: '#f87171',
}

export default function Badge({ label, kind = 'paper' }) {
  const color = COLORS[kind] || COLORS.paper
  return (
    <span
      className="text-[9px] px-1.5 py-0.5 rounded-pill font-mono"
      style={{ backgroundColor: `${color}22`, color }}
    >
      {label}
    </span>
  )
}
