import React, { useEffect } from 'react'
import { AlertTriangle, X } from 'lucide-react'

import { useStore } from './store/useStore'
import ControlPanel from './components/ControlPanel'
import Header from './components/Header'
import MetricsCards from './components/MetricsCards'
import TransactionDetailsDrawer from './components/TransactionDetailsDrawer'
import TransactionTable from './components/TransactionTable'

function ErrorBanner({ message, onDismiss }) {
  return (
    <div
      role="status"
      className="flex items-start gap-2.5 rounded-card border border-status-danger-border bg-status-danger-bg px-3.5 py-2.5 text-xs text-status-danger-text"
    >
      <AlertTriangle className="mt-px h-4 w-4 shrink-0" aria-hidden="true" />
      <p className="flex-1 leading-relaxed">{message}</p>
      <button
        onClick={onDismiss}
        className="shrink-0 rounded p-0.5 transition-colors hover:bg-white/10"
        aria-label="Dismiss message"
      >
        <X className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    </div>
  )
}

// How often the polled panels refresh. Deliberately unhurried: on a free-tier
// worker a chatty dashboard competes with the pipeline it is supposed to watch.
const OPS_POLL_MS = 15000

export default function App() {
  const {
    connectSSE,
    disconnectSSE,
    fetchPolicy,
    hydrateHistory,
    refreshOps,
    isConnected,
    error,
    dismissError,
  } = useStore()

  useEffect(() => {
    fetchPolicy()
    // Load stored traces before the stream opens, so a refresh lands on the
    // history that is already in the database rather than an empty table.
    hydrateHistory()
    refreshOps()
    connectSSE()
    return disconnectSSE
  }, [connectSSE, disconnectSSE, fetchPolicy, hydrateHistory, refreshOps])

  // The issuer radar and retry queue are server-side state with no stream of
  // their own, so they are polled rather than pushed.
  useEffect(() => {
    const id = setInterval(refreshOps, OPS_POLL_MS)
    return () => clearInterval(id)
  }, [refreshOps])

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-surface-0 text-sm text-text-primary lg:flex-row">
      {/* Sidebar — stacks above the content on narrow screens rather than
          squeezing the table into an unreadable column. */}
      <aside className="flex w-full shrink-0 flex-col border-b border-surface-3 bg-surface-1 lg:h-full lg:w-[336px] lg:border-b-0 lg:border-r">
        <Header isConnected={isConnected} />
        <div className="scrollbar-thin flex-1 overflow-y-auto p-4 lg:min-h-0">
          <ControlPanel />
        </div>
      </aside>

      {/* Main content */}
      <main className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
        <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto p-5 lg:p-6">
          {error && <ErrorBanner message={error} onDismiss={dismissError} />}
          <MetricsCards />
          <TransactionTable />
        </div>

        <TransactionDetailsDrawer />
      </main>
    </div>
  )
}
