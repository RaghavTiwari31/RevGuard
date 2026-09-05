import React from 'react'
import { ShieldCheck, Activity } from 'lucide-react'

export default function Header({ isConnected }) {
  return (
    <header className="p-4 border-b border-surface-3 flex items-center justify-between">
      <div className="flex items-center gap-2">
        <div className="bg-brand text-white p-1.5 rounded flex items-center justify-center">
          <ShieldCheck className="w-5 h-5" />
        </div>
        <div>
          <h1 className="text-base font-semibold tracking-tight text-text-primary">
            RevGuard
          </h1>
          <p className="text-[10px] text-text-secondary font-medium uppercase tracking-wider">Triage Engine</p>
        </div>
      </div>
      
      <div className="flex items-center gap-1.5 text-xs font-mono">
        <span className="relative flex h-2 w-2">
          {isConnected && (
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-status-success-text opacity-75"></span>
          )}
          <span className={`relative inline-flex rounded-full h-2 w-2 ${isConnected ? 'bg-status-success-text' : 'bg-surface-4'}`}></span>
        </span>
        <span className={isConnected ? "text-status-success-text" : "text-text-tertiary"}>
          {isConnected ? "ONLINE" : "OFFLINE"}
        </span>
      </div>
    </header>
  )
}
