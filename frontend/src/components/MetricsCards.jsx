import React from 'react'
import { Activity, IndianRupee, ShieldCheck, TrendingUp } from 'lucide-react'

import { useStore } from '../store/useStore'
import { formatDelta, formatINR, formatPct } from '../lib/format'

const EMPTY = {
  total_amount_inr: 0,
  revguard_recovered_inr: 0,
  naive_recovered_inr: 0,
  revguard_yield_pct: 0,
  naive_yield_pct: 0,
  delta_inr: 0,
  total_cost_inr: 0,
  guardrail_adherence_pct: 100,
  guardrail_violations: 0,
}

/** One metric tile. `accent` promotes the headline card without a second layout. */
function Card({ icon: Icon, label, value, valueClass = '', footer, accent = false, progress }) {
  return (
    <div
      className={`panel relative flex flex-col overflow-hidden p-4 ${
        accent ? 'border-brand-500/30 bg-brand-500/[0.06]' : ''
      }`}
    >
      {/* Determinate progress rail — only rendered mid-run. */}
      {typeof progress === 'number' && (
        <div
          className="absolute inset-x-0 top-0 h-0.5 bg-brand-500 transition-[width] duration-500 ease-out"
          style={{ width: `${progress}%` }}
          role="progressbar"
          aria-valuenow={Math.round(progress)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Batch progress"
        />
      )}

      <div className="flex items-center justify-between gap-2">
        <span className={`label ${accent ? 'text-brand-light' : ''}`}>{label}</span>
        <Icon className="h-3.5 w-3.5 shrink-0 opacity-40" aria-hidden="true" />
      </div>

      <div
        className={`tabular mt-2 text-[26px] font-semibold leading-none tracking-[-0.02em] ${
          valueClass || 'text-text-primary'
        }`}
      >
        {value}
      </div>

      <div className="mt-2.5 text-2xs text-text-tertiary">{footer}</div>
    </div>
  )
}

export default function MetricsCards() {
  const { summary, isRunning, progress, total } = useStore()
  const data = summary ?? EMPTY

  const progressPct = total > 0 ? Math.min(100, (progress / total) * 100) : 0
  const netRoi = (data.revguard_recovered_inr || 0) - (data.total_cost_inr || 0)
  const clean = !data.guardrail_violations

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <Card
        icon={Activity}
        label="Batch Volume"
        value={formatINR(data.total_amount_inr)}
        progress={isRunning ? progressPct : undefined}
        footer={
          isRunning ? (
            <span className="tabular text-brand-light">
              Processing {progress} of {total}
            </span>
          ) : summary ? (
            <span className="tabular">{data.total_records ?? 0} records processed</span>
          ) : (
            'Awaiting batch run'
          )
        }
      />

      <Card
        accent
        icon={TrendingUp}
        label="Expected Recovery"
        value={formatINR(data.revguard_recovered_inr)}
        valueClass="text-white"
        footer={
          <span className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
            <span className="pill border-brand-500/30 bg-brand-500/15 text-brand-light">
              {formatPct(data.revguard_yield_pct)} yield
            </span>
            <span>vs naive cron</span>
            <span className="tabular line-through decoration-text-tertiary/60">
              {formatINR(data.naive_recovered_inr)}
            </span>
            <span className="tabular font-semibold text-status-success-text">
              {formatDelta(data.delta_inr)}
            </span>
          </span>
        }
      />

      <Card
        icon={IndianRupee}
        label="Dispatch Cost"
        value={formatINR(data.total_cost_inr)}
        footer={
          <span>
            Net ROI{' '}
            <span
              className={`tabular font-semibold ${
                netRoi < 0 ? 'text-status-danger-text' : 'text-status-success-text'
              }`}
            >
              {formatDelta(netRoi)}
            </span>
          </span>
        }
      />

      <Card
        icon={ShieldCheck}
        label="Guardrail Adherence"
        value={formatPct(data.guardrail_adherence_pct)}
        valueClass={clean ? 'text-status-success-text' : 'text-status-danger-text'}
        footer={
          clean
            ? 'No guardrail breaches'
            : `${data.guardrail_violations} violation${
                data.guardrail_violations === 1 ? '' : 's'
              } blocked`
        }
      />
    </div>
  )
}
