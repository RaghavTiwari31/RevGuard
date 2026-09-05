import React from 'react'
import ReactDOM from 'react-dom/client'

// Self-hosted Inter (variable weight axis). Bundled rather than pulled from a
// CDN so the dashboard renders identically offline and on first paint — no
// flash of fallback text, no third-party request on the demo path.
import '@fontsource-variable/inter'

import App from './App'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
