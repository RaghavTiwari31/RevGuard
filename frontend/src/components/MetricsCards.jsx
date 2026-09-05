import React from 'react'
import { useStore } from '../store/useStore'
import { TrendingUp, ShieldAlert, IndianRupee, Activity } from 'lucide-react'

const formatINR = (val) => new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  maximumFractionDigits: 0
}).format(val || 0);

export default function MetricsCards() {
  const { summary, isRunning, progress, total } = useStore()
  
  const data = summary || {
    total_amount_inr: 0,
    revguard_recovered_inr: 0,
    naive_recovered_inr: 0,
    revguard_yield_pct: 0,
    naive_yield_pct: 0,
    delta_inr: 0,
    total_cost_inr: 0,
    guardrail_adherence_pct: 100,
    guardrail_violations: 0
  }

  const progressPct = total > 0 ? Math.round((progress / total) * 100) : 0;

  return (
    <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
      {/* Card 1: Batch Progress / Total Volume */}
      <div className="panel p-4 flex flex-col gap-1 relative overflow-hidden">
        {isRunning && (
          <div 
            className="absolute top-0 left-0 h-0.5 bg-brand transition-all duration-300 ease-out" 
            style={{ width: `${progressPct}%` }}
          />
        )}
        <div className="text-text-secondary text-xs font-medium uppercase tracking-wider flex items-center justify-between">
          Batch Volume
          <Activity className="w-3.5 h-3.5 opacity-50" />
        </div>
        <div className="text-2xl font-semibold tracking-tight text-text-primary mt-1">
          {formatINR(data.total_amount_inr)}
        </div>
        <div className="text-xs text-text-tertiary mt-2">
          {isRunning ? `Processing ${progress}/${total}` : '100% Processed'}
        </div>
      </div>

      {/* Card 2: RevGuard Yield (Shadow Ledger) */}
      <div className="panel p-4 flex flex-col gap-1 border-brand/30 bg-brand/5 relative">
        <div className="absolute left-0 top-0 bottom-0 w-0.5 bg-brand/50 rounded-l" />
        <div className="text-brand-light text-xs font-medium uppercase tracking-wider flex items-center justify-between">
          Expected Recovery
          <TrendingUp className="w-3.5 h-3.5 opacity-50" />
        </div>
        <div className="flex items-baseline gap-2 mt-1">
          <div className="text-2xl font-semibold tracking-tight text-white">
            {formatINR(data.revguard_recovered_inr)}
          </div>
          <div className="text-[10px] font-medium bg-brand/20 text-brand-light px-1.5 py-0.5 rounded">
            {data.revguard_yield_pct}% Yield
          </div>
        </div>
        <div className="text-xs text-text-tertiary mt-2 flex items-center gap-1">
          <span>vs Naive Cron:</span>
          <span className="line-through">{formatINR(data.naive_recovered_inr)}</span>
          <span className="text-status-success-text font-medium">+{formatINR(data.delta_inr)}</span>
        </div>
      </div>

      {/* Card 3: Execution Cost */}
      <div className="panel p-4 flex flex-col gap-1">
        <div className="text-text-secondary text-xs font-medium uppercase tracking-wider flex items-center justify-between">
          Dispatch Cost
          <IndianRupee className="w-3.5 h-3.5 opacity-50" />
        </div>
        <div className="text-2xl font-semibold tracking-tight text-text-primary mt-1">
          {formatINR(data.total_cost_inr)}
        </div>
        <div className="text-xs text-text-tertiary mt-2">
          Net ROI: <span className="text-status-success-text font-medium">+{formatINR(data.revguard_recovered_inr - data.total_cost_inr)}</span>
        </div>
      </div>

      {/* Card 4: Guardrail Adherence */}
      <div className="panel p-4 flex flex-col gap-1">
        <div className="text-text-secondary text-xs font-medium uppercase tracking-wider flex items-center justify-between">
          Guardrail Adherence
          <ShieldAlert className={`w-3.5 h-3.5 opacity-50 ${data.guardrail_violations > 0 ? 'text-status-danger-text' : ''}`} />
        </div>
        <div className={`text-2xl font-semibold tracking-tight mt-1 ${data.guardrail_violations > 0 ? 'text-status-danger-text' : 'text-status-success-text'}`}>
          {data.guardrail_adherence_pct}%
        </div>
        <div className="text-xs text-text-tertiary mt-2">
          {data.guardrail_violations} Violations Blocked
        </div>
      </div>
    </div>
  )
}
