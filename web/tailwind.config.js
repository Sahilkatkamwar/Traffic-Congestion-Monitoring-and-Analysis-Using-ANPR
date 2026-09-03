/** @type {import('tailwindcss').Config} */
//
// Every value here reads a custom property from src/tokens.css. The palette is
// defined once, in CSS, and Tailwind is only the way it gets addressed.
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        surface: {
          0: 'var(--surface-0)',
          1: 'var(--surface-1)',
          2: 'var(--surface-2)',
          3: 'var(--surface-3)',
        },
        ink: {
          hi: 'var(--ink-hi)',
          mid: 'var(--ink-mid)',
          low: 'var(--ink-low)',
        },
        // Indian plate grounds. These carry meaning and are never decoration.
        plate: {
          white: 'var(--plate-white)',
          yellow: 'var(--plate-yellow)',
          green: 'var(--plate-green)',
          red: 'var(--plate-red)',
        },
        hairline: 'var(--hairline)',
      },
      fontFamily: {
        sans: 'var(--font-sans)',
        plate: 'var(--font-plate)',
      },
      borderRadius: {
        card: '14px',
        control: '8px',
      },
      boxShadow: {
        float: 'var(--shadow-float)',
        lift: 'var(--shadow-lift)',
      },
      letterSpacing: {
        plate: '0.14em',
        label: '0.11em',
      },
      fontSize: {
        label: ['10.5px', { lineHeight: '1.2', letterSpacing: '0.11em' }],
        body: ['14.5px', { lineHeight: '1.55' }],
        count: ['34px', { lineHeight: '1.05', letterSpacing: '-0.02em' }],
      },
    },
  },
  plugins: [],
}
