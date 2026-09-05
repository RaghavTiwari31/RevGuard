import React from 'react'
import { ShieldCheck } from 'lucide-react'

export default function Header({ isConnected }) {
  return (
    <header className="flex items-center justify-between gap-3 border-b border-surface-3 px-4 py-4">
      <div className="flex items-center gap-2.5">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-brand-400 to-brand-600 text-white shadow-raised">
          <ShieldCheck className="h-[18px] w-[18px]" strokeWidth={2.25} />
        </div>
        <div className="leading-tight">
          <h1 className="text-[15px] font-semibold tracking-[-0.01em] text-text-primary">
            RevGuard
          </h1>
          <p className="label text-text-tertiary">Triage Engine</p>
        </div>
      </div>

      <div
        className={`pill ${
          isConnected
            ? 'border-status-success-border bg-status-success-bg text-status-success-text'
            : 'border-surface-3 bg-surface-2 text-text-tertiary'
        }`}
        title={
          isConnected
            ? 'Live event stream connected'
            : 'Event stream disconnected — retrying automatically'
        }
      >
        <span className="relative flex h-1.5 w-1.5" aria-hidden="true">
          {isConnected && (
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-status-success-text opacity-75" />
          )}
          <span
            className={`relative inline-flex h-1.5 w-1.5 rounded-full ${
              isConnected ? 'bg-status-success-text' : 'bg-surface-5'
            }`}
          />
        </span>
        <span className="tracking-label">{isConnected ? 'LIVE' : 'OFFLINE'}</span>
      </div>
    </header>
  )
}
