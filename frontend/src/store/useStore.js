import { create } from 'zustand'

// In production (Vercel), VITE_API_BASE_URL is set to the Render backend URL.
// In local dev it is empty, so paths stay relative and the Vite proxy handles them.
const API = import.meta.env.VITE_API_BASE_URL || ''

// Held outside the store: this is a live connection handle, not render state.
// Keeping it here also lets connectSSE() be idempotent across React StrictMode's
// double-invoked effects, which would otherwise open two EventSources and show
// every trace twice.
let eventSource = null

const MAX_EVENTS = 500 // Cap retained rows so a long run cannot grow unbounded.

export const useStore = create((set, get) => ({
  // Connection state
  isConnected: false,
  error: null,

  // Batch run state
  runId: null,
  progress: 0,
  total: 0,
  isRunning: false,

  // Data
  events: [], // Trace events, newest first — history on load, then live
  summary: null, // Batch summary incl. shadow ledger + bandit stats
  policy: null, // Currently active policy
  selectedEvent: null, // Drawer target

  // Server-side state, polled rather than streamed
  issuers: null, // Issuer Health Radar
  retries: null, // Durable retry queue
  abResult: null, // Last A/B comparison
  isHydrating: false,
  historyTotal: 0,

  // ── SSE ────────────────────────────────────────────────────────────────────

  connectSSE: () => {
    // Guard on the handle, not on isConnected: isConnected only flips to true
    // once the server responds, so two effects firing back-to-back would both
    // sail past a flag-based check.
    if (eventSource) return

    eventSource = new EventSource(`${API}/stream`)

    eventSource.onopen = () => set({ isConnected: true, error: null })

    // EventSource reconnects on its own; surface the gap without tearing down.
    eventSource.onerror = () =>
      set({ isConnected: false, error: 'Stream disconnected — retrying…' })

    eventSource.onmessage = (e) => {
      let data
      try {
        data = JSON.parse(e.data)
      } catch {
        // Heartbeats arrive as SSE comments and never reach onmessage, so
        // anything unparseable here is genuinely malformed.
        console.error('Failed to parse SSE message', e.data)
        return
      }

      switch (data.type) {
        case 'batch_start':
          set({
            runId: data.run_id,
            total: data.total ?? 0,
            progress: 0,
            isRunning: true,
            events: [],
            summary: null,
            selectedEvent: null,
            error: null,
          })
          break

        case 'trace_update':
          set((state) => ({
            events: [
              { ...data, timestamp: data.timestamp || new Date().toISOString() },
              ...state.events,
            ].slice(0, MAX_EVENTS),
            progress: data.index ?? state.progress,
            total: data.total ?? state.total,
          }))
          break

        case 'batch_progress':
          set((state) => ({
            summary: data.summary ?? state.summary,
            progress: data.progress ?? state.progress,
            total: data.total ?? state.total,
            isRunning: true,
          }))
          break

        case 'batch_complete':
          set((state) => ({
            summary: data.summary ?? state.summary,
            progress: data.progress ?? state.progress,
            total: data.total ?? state.total,
            isRunning: false,
          }))
          get().refreshOps()
          break

        case 'batch_failed':
          set({
            isRunning: false,
            error: data.error || 'Batch run failed',
          })
          break

        case 'ab_start':
          set({ runId: data.run_id, isRunning: true, abResult: null, error: null })
          break

        case 'ab_complete':
          set({ abResult: data.result, isRunning: false })
          break

        case 'automation_frozen':
          // A customer disputed or opted out. Surface it prominently — this is
          // the one event where the right outcome is the system stopping.
          set({
            error:
              `Automation frozen for ${data.event_id || 'a customer'} — ` +
              `${data.category?.replace(/_/g, ' ').toLowerCase()}` +
              (data.cancelled_retries
                ? `, ${data.cancelled_retries} retry cancelled`
                : ''),
          })
          get().refreshOps()
          break

        default:
          break
      }
    }
  },

  disconnectSSE: () => {
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
    set({ isConnected: false })
  },

  // ── History ────────────────────────────────────────────────────────────────

  /**
   * Load stored traces so a refresh does not start from an empty table.
   *
   * Historical rows are serialised in the same shape as live SSE events, so
   * they drop straight into the same list with no special-casing.
   */
  hydrateHistory: async (limit = 100) => {
    set({ isHydrating: true })
    try {
      const res = await fetch(`${API}/traces?limit=${limit}`)
      if (!res.ok) throw new Error(`Failed to load history (${res.status})`)
      const data = await res.json()
      set((state) => ({
        // Never clobber events that arrived over SSE while this was in flight.
        events: state.events.length > 0 ? state.events : data.traces,
        historyTotal: data.total,
        isHydrating: false,
      }))
    } catch (err) {
      console.error('Failed to hydrate history', err)
      set({ isHydrating: false })
    }
  },

  /** Refresh the polled server-side panels (issuer radar, retry queue). */
  refreshOps: async () => {
    const [issuers, retries] = await Promise.allSettled([
      fetch(`${API}/issuers`).then((r) => (r.ok ? r.json() : null)),
      fetch(`${API}/retries`).then((r) => (r.ok ? r.json() : null)),
    ])
    set({
      issuers: issuers.status === 'fulfilled' ? issuers.value : null,
      retries: retries.status === 'fulfilled' ? retries.value : null,
    })
  },

  // ── Simulation ─────────────────────────────────────────────────────────────

  startSimulation: async (seed = 42) => {
    // Flip immediately rather than waiting for batch_start to come back over
    // SSE, so the button cannot be double-clicked into two concurrent runs.
    set({ isRunning: true, error: null })
    try {
      const res = await fetch(`${API}/simulate?seed=${Number(seed) || 0}`, {
        method: 'POST',
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || `Failed to start simulation (${res.status})`)
      set({ runId: data.run_id ?? null, abResult: null })
    } catch (err) {
      set({ error: err.message, isRunning: false })
    }
  },

  /**
   * Run both channel-selection strategies over identical data.
   *
   * `warm` pre-trains the bandit first: cold measures the price of exploration,
   * warm measures steady state.
   */
  startAbComparison: async (seed = 42, warm = false) => {
    set({ isRunning: true, error: null, abResult: null })
    try {
      const res = await fetch(
        `${API}/simulate/ab?seed=${Number(seed) || 0}&warm=${warm}`,
        { method: 'POST' },
      )
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || `Failed to start comparison (${res.status})`)
      set({ runId: data.run_id ?? null })
    } catch (err) {
      set({ error: err.message, isRunning: false })
    }
  },

  cancelRetry: async (retryId) => {
    try {
      const res = await fetch(`${API}/retries/${retryId}/cancel`, { method: 'POST' })
      if (!res.ok) throw new Error(`Could not cancel retry (${res.status})`)
      await get().refreshOps()
      return true
    } catch (err) {
      set({ error: err.message })
      return false
    }
  },

  runRetryNow: async (retryId) => {
    try {
      const res = await fetch(`${API}/retries/${retryId}/run`, { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `Could not run retry (${res.status})`)
      await get().refreshOps()
      return true
    } catch (err) {
      set({ error: err.message })
      return false
    }
  },

  // ── Policy ─────────────────────────────────────────────────────────────────

  fetchPolicy: async () => {
    try {
      const res = await fetch(`${API}/policy`)
      if (!res.ok) throw new Error(`Failed to load policy (${res.status})`)
      set({ policy: await res.json() })
    } catch (err) {
      console.error('Failed to fetch policy', err)
      set({ error: err.message })
    }
  },

  updatePolicy: async (patch) => {
    try {
      const res = await fetch(`${API}/policy/update`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      })
      const data = await res.json().catch(() => ({}))
      // The backend rejects an invalid patch wholesale and leaves the live
      // policy untouched, so surface the reason instead of silently no-opping.
      if (!res.ok) throw new Error(data.detail || `Policy update rejected (${res.status})`)
      set({ policy: data.policy, error: null })
      return true
    } catch (err) {
      console.error('Failed to update policy', err)
      set({ error: err.message })
      return false
    }
  },

  resetPolicy: async () => {
    try {
      const res = await fetch(`${API}/policy/reset`, { method: 'POST' })
      if (!res.ok) throw new Error(`Policy reset failed (${res.status})`)
      const data = await res.json()
      set({ policy: data.policy, error: null })
      return true
    } catch (err) {
      set({ error: err.message })
      return false
    }
  },

  // ── UI ─────────────────────────────────────────────────────────────────────

  setSelectedEvent: (event) => set({ selectedEvent: event }),
  dismissError: () => set({ error: null }),
}))
