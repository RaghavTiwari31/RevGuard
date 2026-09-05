import React from 'react'
import { Landmark } from 'lucide-react'

import { formatPct } from '../lib/format'

const HEALTH_STYLES = {
  outage: {
    label: 'Outage',
    pill: 'border-status-danger-border bg-status-danger-bg text-status-danger-text',
    bar: 'bg-status-danger-text',
  },
  degraded: {
    label: 'Degraded',
    pill: 'border-status-warning-border bg-status-warning-bg text-status-warning-text',
    bar: 'bg-status-warning-text',
  },
  healthy: {
    label: 'Healthy',
    pill: 'border-status-neutral-border bg-status-neutral-bg text-text-tertiary',
    bar: 'bg-status-success-text',
  },
}

const formatCountdown = (seconds) => {
  if (!seconds || seconds <= 0) return null
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

/**
 * Surfaces the Issuer Health Radar — the per-BIN failure counter that puts an
 * issuer into extended backoff during an outage. The engine has always acted on
 * this; it was simply never visible.
 */
export default function IssuerRadar({ data }) {
  const issuers = data?.issuers ?? []

  if (issuers.length === 0) {
    return (
      <p className="py-3 text-center text-xs text-text-tertiary">
        No issuer traffic yet.
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-center justify-between text-2xs text-text-tertiary">
        <span className="tabular">{data.total_tracked} tracked</span>
        {data.in_outage > 0 ? (
          <span className="pill border-status-danger-border bg-status-danger-bg text-status-danger-text">
            {data.in_outage} in outage
          </span>
        ) : (
          <span className="tabular">
            trips at {data.spike_threshold}/{data.window_minutes}min
          </span>
        )}
      </div>

      <ul className="flex flex-col gap-2">
        {issuers.slice(0, 6).map((issuer) => {
          const style = HEALTH_STYLES[issuer.health] ?? HEALTH_STYLES.healthy
          const countdown = formatCountdown(issuer.backoff_seconds_remaining)

          return (
            <li key={issuer.bin} className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-1.5">
                  <Landmark
                    className="h-3 w-3 shrink-0 text-text-tertiary"
                    aria-hidden="true"
                  />
                  <span className="font-mono text-xs text-text-primary">{issuer.bin}</span>
                </span>
                <span className={`pill ${style.pill}`}>
                  {style.label}
                  {countdown && <span className="tabular"> · {countdown}</span>}
                </span>
              </div>

              {/* How close this issuer is to tripping the spike threshold. */}
              <div
                className="h-1 w-full overflow-hidden rounded-full bg-surface-3"
                role="progressbar"
                aria-valuenow={Math.round(issuer.pressure * 100)}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={`BIN ${issuer.bin} failure pressure`}
              >
                <div
                  className={`h-full rounded-full transition-all duration-500 ${style.bar}`}
                  style={{ width: `${Math.max(2, issuer.pressure * 100)}%` }}
                />
              </div>

              <div className="tabular flex justify-between text-[10px] text-text-tertiary">
                <span>
                  {issuer.rolling_failures}/{issuer.spike_threshold} in window
                </span>
                <span>{formatPct(issuer.pressure * 100, 0)} to backoff</span>
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
