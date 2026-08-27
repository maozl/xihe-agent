/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./src/renderer/index.html', './src/renderer/src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: 'rgb(var(--c-brand) / <alpha-value>)',
          soft: '#3a4a7a'
        },
        // sky-family accent for workspace/path marks; sky-300 etc. are tuned
        // for dark surfaces, so this token flips per theme instead.
        accent: 'rgb(var(--c-accent) / <alpha-value>)',
        // Status hues flip per theme like accent: text/border/tint usages go
        // through these. Solid fills paired with white text (bg-*-600 class
        // buttons) keep the fixed mid-tone palette — it reads on both themes.
        success: 'rgb(var(--c-success) / <alpha-value>)',
        warning: 'rgb(var(--c-warning) / <alpha-value>)',
        danger: 'rgb(var(--c-danger) / <alpha-value>)',
        // Semantic theme tokens — values are CSS variables set per theme in
        // index.css (one html[data-theme='<name>'] block per appearance,
        // `:root` = dark defaults), so one class serves every theme (no
        // dark: variants). <alpha-value> keeps /60 etc working.
        // contrast intentionally does NOT flip: it backs white/light text
        // (chat bubble, active tab) which must stay readable in every theme.
        app: 'rgb(var(--c-app) / <alpha-value>)',
        panel: 'rgb(var(--c-panel) / <alpha-value>)',
        elevated: 'rgb(var(--c-elevated) / <alpha-value>)',
        strong: 'rgb(var(--c-strong) / <alpha-value>)',
        contrast: 'rgb(var(--c-contrast) / <alpha-value>)',
        ink: 'rgb(var(--c-ink) / <alpha-value>)',
        'ink-2': 'rgb(var(--c-ink-2) / <alpha-value>)',
        'ink-3': 'rgb(var(--c-ink-3) / <alpha-value>)',
        'ink-4': 'rgb(var(--c-ink-4) / <alpha-value>)',
        line: 'rgb(var(--c-line) / <alpha-value>)',
        'line-strong': 'rgb(var(--c-line-strong) / <alpha-value>)'
      }
    }
  },
  plugins: []
}
