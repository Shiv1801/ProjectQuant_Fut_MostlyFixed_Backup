/** Design tokens locked per PRD §7.5 — do not deviate. */
module.exports = {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0a0e14",
        card: "#131820",
        cardInset: "#0e1218",
        groupHeader: "#161c26",
        borderDefault: "#232b38",
        borderSubtle: "#1a202b",
        textPrimary: "#d4d9e0",
        textMuted: "#6b7688",
        textSecondary: "#9ca8b8",
        positive: "#4ade80",
        negative: "#f87171",
        warning: "#fbbf24",
        accent: "#4fd1c5",
        infoPaper: "#60a5fa",
        strikeHighlight: "#e2c96a",
      },
      fontFamily: { mono: ["Consolas", "Menlo", "monospace"] },
      borderRadius: { card: "4px", pill: "3px" },
    },
  },
  plugins: [],
}
