/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // Neutral ramp. Slightly blue-shifted rather than pure grey so the
        // indigo brand colour sits on it without looking like a sticker.
        surface: {
          0: '#0b0c0f', // App background
          1: '#111318', // Sidebar / control panels
          2: '#16181e', // Cards / table surface
          3: '#22252e', // Borders / hover
          4: '#333846', // Prominent borders
          5: '#4a5063', // Disabled text / muted marks
        },
        text: {
          primary: '#f2f3f7',
          secondary: '#a2a8b8',
          tertiary: '#6f7688',
        },
        brand: {
          50: '#eef1ff',
          100: '#e0e5ff',
          200: '#c6cfff',
          300: '#a3b0fd',
          400: '#818cf8',
          500: '#6366f1',
          600: '#4f46e5',
          700: '#4338ca',
          light: '#c6cfff',
          DEFAULT: '#6366f1',
          dark: '#3730a3',
        },
        status: {
          success: { bg: '#0a2a18', border: '#17512f', text: '#4ade80' },
          warning: { bg: '#2c1a05', border: '#5c3a0d', text: '#fbbf24' },
          danger: { bg: '#2d0f10', border: '#5f1f21', text: '#f87171' },
          info: { bg: '#08283f', border: '#0f4b70', text: '#38bdf8' },
          neutral: { bg: '#1c1f27', border: '#2e323d', text: '#a2a8b8' },
        },
        // Per-channel accents, reused by the table, drawer and bandit chart so
        // "WhatsApp" is the same green everywhere it appears.
        channel: {
          whatsapp: '#25d366',
          sms: '#38bdf8',
          voice: '#a78bfa',
        },
      },
      fontFamily: {
        // 'Inter Variable' is the family name shipped by @fontsource-variable/inter,
        // imported in main.jsx. The rest is a real fallback stack, not decoration.
        sans: [
          'Inter Variable',
          'Inter',
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
        mono: [
          'ui-monospace',
          'SFMono-Regular',
          'JetBrains Mono',
          'Menlo',
          'Consolas',
          'monospace',
        ],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem', letterSpacing: '0.01em' }],
      },
      letterSpacing: {
        label: '0.06em',
      },
      borderRadius: {
        card: '0.625rem',
      },
      boxShadow: {
        card: '0 1px 2px 0 rgb(0 0 0 / 0.4)',
        raised: '0 4px 12px -2px rgb(0 0 0 / 0.5), 0 2px 4px -2px rgb(0 0 0 / 0.3)',
        drawer: '-16px 0 40px -12px rgb(0 0 0 / 0.7)',
        'focus-brand': '0 0 0 2px #0b0c0f, 0 0 0 4px #6366f1',
      },
      animation: {
        'slide-in': 'slideIn 0.22s cubic-bezier(0.16, 1, 0.3, 1)',
        'fade-in': 'fadeIn 0.15s ease-out',
        'row-in': 'rowIn 0.3s cubic-bezier(0.16, 1, 0.3, 1)',
        shimmer: 'shimmer 1.6s ease-in-out infinite',
      },
      keyframes: {
        slideIn: {
          from: { transform: 'translateX(24px)', opacity: 0 },
          to: { transform: 'translateX(0)', opacity: 1 },
        },
        fadeIn: {
          from: { opacity: 0 },
          to: { opacity: 1 },
        },
        rowIn: {
          from: { opacity: 0, transform: 'translateY(-4px)' },
          to: { opacity: 1, transform: 'translateY(0)' },
        },
        shimmer: {
          '0%, 100%': { opacity: 0.35 },
          '50%': { opacity: 0.7 },
        },
      },
    },
  },
  plugins: [],
}
