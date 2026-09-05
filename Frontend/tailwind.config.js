/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // 00_SHARED_CONTRACT.md §1.3 risk bands, as design tokens.
        risk: {
          low: '#22C55E',
          moderate: '#EAB308',
          high: '#F97316',
          critical: '#EF4444',
        },
        surface: {
          900: '#0B0F17',
          800: '#111725',
          700: '#182031',
          600: '#232D42',
        },
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
};
