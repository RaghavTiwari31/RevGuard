import React, { useEffect } from 'react'
import { useStore } from './store/useStore'
import Header from './components/Header'
import MetricsCards from './components/MetricsCards'
import TransactionTable from './components/TransactionTable'
import ControlPanel from './components/ControlPanel'
import TransactionDetailsDrawer from './components/TransactionDetailsDrawer'

function App() {
  const { connectSSE, fetchPolicy, isConnected } = useStore()

  useEffect(() => {
    fetchPolicy()
    connectSSE()
  }, [])

  return (
    <div className="flex h-screen overflow-hidden bg-surface-0 text-text-primary text-sm">
      
      {/* Sidebar (Left) */}
      <div className="w-[340px] border-r border-surface-3 bg-surface-1 flex flex-col flex-shrink-0">
        <Header isConnected={isConnected} />
        <div className="flex-1 overflow-y-auto p-4 scrollbar-hide">
          <ControlPanel />
        </div>
      </div>
      
      {/* Main Content (Right) */}
      <div className="flex-1 flex flex-col h-full overflow-hidden relative">
        <div className="flex-1 overflow-y-auto p-6 flex flex-col gap-6">
          <MetricsCards />
          <TransactionTable />
        </div>
        
        {/* Slide-out Drawer */}
        <TransactionDetailsDrawer />
      </div>
      
    </div>
  )
}

export default App
