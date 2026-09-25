/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  darkMode: 'class',
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
          950: '#F5EFE6', // Main page background (Beige)
          900: '#FAF5ED', // Panels & backgrounds
          850: '#FDFBF7', // Panels
          800: '#FEFDFB', // Elevated cards
          750: '#F5EFE6', // Accents
          700: '#D5C9B8', // Borders & dividers
          650: '#BAA892', // Hover states
          600: '#FDFBF7', // Subtle button backgrounds
          500: '#8C7B65', // Darker
        },
        slate: {
          50: '#020617',
          100: '#0f172a',
          200: '#1e293b', // Primary text
          300: '#334155', // Secondary text
          400: '#475569', // Tertiary text
          500: '#64748B',
          600: '#94A3B8',
          700: '#CBD5E1',
          800: '#E2E8F0',
          900: '#F1F5F9',
          950: '#F8FAFC',
        },
        brand: {
          DEFAULT: '#0D9488', // teal-600
          50: '#F0FDFA',
          400: '#2DD4BF',
          500: '#14B8A6',
          600: '#0D9488',
          700: '#0F766E',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      boxShadow: {
        'panel': '0 1px 3px rgba(0, 0, 0, 0.4), 0 4px 12px rgba(0, 0, 0, 0.25)',
        'panel-elevated': '0 4px 20px rgba(0, 0, 0, 0.5), 0 0 1px rgba(255, 255, 255, 0.1)',
        'glow-cyan': '0 0 15px rgba(20, 184, 166, 0.25)',
      },
    },
  },
  plugins: [],
};
