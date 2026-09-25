# UI Design Tokens (PRD §7.5 — locked, not a style reference)

Mirrored in `frontend/tailwind.config.js`. Do not deviate — "not a mood
board", "deviating toward a friendlier consumer-app look would work
against the density and seriousness this tool needs to convey."

| Token | Hex | Usage |
|---|---|---|
| Page background | #0a0e14 | Outermost background |
| Card/panel background | #131820 | Every card, table container |
| Card background (inset) | #0e1218 | Stat boxes, nested panels |
| Group header background | #161c26 | Collapsible section headers |
| Border (default) | #232b38 | All card/table borders |
| Border (subtle row divider) | #1a202b | Table row dividers |
| Primary text | #d4d9e0 | Default body text |
| Muted/label text | #6b7688 | Labels, secondary info |
| Positive/up/win | #4ade80 | Gains, confirmations, LIVE badge |
| Negative/down/loss | #f87171 | Losses, hard SL |
| Warning/neutral-caution | #fbbf24 | Elevated correlation, ambiguous states |
| Accent | #4fd1c5 | Active tab text, signal markers |
| Info/Paper mode | #60a5fa | PAPER badge |
| Strike/highlight | #e2c96a | ATM strike row |

Typography: `'Consolas', 'Menlo', monospace` throughout, no exceptions.
No gradients, no drop shadows, no rounded pill-shaped cards, no light mode.
