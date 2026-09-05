/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        surface: {
          0: '#0a0a0a',   // App Background
          1: '#121212',   // Sidebar / Control Panels
          2: '#171717',   // Main Cards / Table Rows
          3: '#262626',   // Borders / Hover States
          4: '#404040',   // Prominent Borders
        },
        text: {
          primary: '#f5f5f5',
          secondary: '#a3a3a3',
          tertiary: '#737373',
        },
        brand: {
          light: '#e0e7ff',
          DEFAULT: '#6366f1', // Muted Indigo
          dark: '#3730a3',
        },
        status: {
          success: {
            bg: '#052e16',
            border: '#14532d',
            text: '#4ade80'
          },
          warning: {
            bg: '#451a03',
            border: '#78350f',
            text: '#fbbf24'
          },
          danger: {
            bg: '#450a0a',
            border: '#7f1d1d',
            text: '#f87171'
          },
          info: {
            bg: '#082f49',
            border: '#0c4a6e',
            text: '#38bdf8'
          }
        }
      },
      fontFamily: {
        sans: ['Inter var', 'Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
      },
      animation: {
        'slide-in': 'slideIn 0.2s ease-out',
        'fade-in': 'fadeIn 0.15s ease-out',
      },
      keyframes: {
        slideIn: {
          from: { transform: 'translateX(20px)', opacity: 0 },
          to: { transform: 'translateX(0)', opacity: 1 },
        },
        fadeIn: {
          from: { opacity: 0 },
          to: { opacity: 1 },
        },
      },
    },
  },
  plugins: [],
}
