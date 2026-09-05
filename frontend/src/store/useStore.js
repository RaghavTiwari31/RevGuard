import { create } from 'zustand'

// In production (Vercel), VITE_API_BASE_URL is set to the Render backend URL.
// In local dev, it is empty so paths are relative (proxied by Vite).
const API = import.meta.env.VITE_API_BASE_URL || ''

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
  events: [],       // Live trace events stream
  summary: null,    // Batch summary including shadow ledger and bandit
  policy: null,     // Current active policy
  selectedEvent: null, // For details drawer

  // Actions
  connectSSE: () => {
    if (get().isConnected) return;
    
    const eventSource = new EventSource(`${API}/stream`);
    
    eventSource.onopen = () => set({ isConnected: true, error: null });
    eventSource.onerror = () => set({ isConnected: false, error: 'SSE connection lost' });
    
    eventSource.onmessage = (e) => {
      if (e.data === ': heartbeat') return;
      
      try {
        const data = JSON.parse(e.data);
        
        switch (data.type) {
          case 'batch_start':
            set({ 
              runId: data.run_id, 
              total: data.total, 
              progress: 0,
              isRunning: true,
              events: [],
              summary: null,
              selectedEvent: null
            });
            break;
            
          case 'trace_update':
            set((state) => ({
              events: [{
                ...data,
                timestamp: data.timestamp || new Date().toISOString()
              }, ...state.events],
              progress: data.index || state.progress
            }));
            break;
            
          case 'batch_progress':
          case 'batch_complete':
            set({ 
              summary: data.summary,
              progress: data.progress || get().progress,
              isRunning: data.type === 'batch_progress' 
            });
            break;
        }
      } catch (err) {
        console.error("Failed to parse SSE message", err);
      }
    };
  },

  startSimulation: async (seed = 42) => {
    try {
      const res = await fetch(`${API}/simulate?seed=${seed}`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to start simulation');
    } catch (err) {
      set({ error: err.message });
    }
  },

  fetchPolicy: async () => {
    try {
      const res = await fetch(`${API}/policy`);
      if (res.ok) {
        const data = await res.json();
        set({ policy: data });
      }
    } catch (err) {
      console.error("Failed to fetch policy", err);
    }
  },
  
  updatePolicy: async (patch) => {
    try {
      const res = await fetch(`${API}/policy/update`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch)
      });
      if (res.ok) {
        const data = await res.json();
        set({ policy: data.policy });
      }
    } catch (err) {
      console.error("Failed to update policy", err);
    }
  },

  setSelectedEvent: (event) => set({ selectedEvent: event })
}))
