import React from 'react'
import { AlertCircle, Play, X } from 'lucide-react'

import { useStore } from '../store/useStore'

const STATUS_STYLES = {
  pending: 'border-status-info-border bg-status-info-bg text-status-info-text',
  running: 'border-status-warning-border bg-status-warning-bg text-status-warning-text',
  completed: 'border-status-success-border bg-status-success-bg text-status-success-text',
  failed: 'border-status-danger-border bg-status-danger-bg text-status-danger-text',
  cancelled: 'border-status-neutral-border bg-status-neutral-bg text-text-tertiary',
}

const formatDue = (seconds) => {
  if (seconds === null || seconds === undefined) return null
  if (seconds <= 0) return 'due now'
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`
}

/**
 * The durable retry queue.
 *
 * Retries are persisted and rearmed at startup, so this panel is also the
 * evidence that a restart did not quietly drop pending work.
 */
export default function RetryQueue({ data }) {
  const { cancelRetry, runRetryNow } = useStore()

  const retries = data?.retries ?? []
  const pending = data?.pending ?? 0

  if (retries.length === 0) {
    return (
      <p className="py-3 text-center text-xs text-text-tertiary">
        No retries scheduled.
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-center justify-between text-2xs text-text-tertiary">
        <span className="tabular">{pending} pending</span>
        {data?.scheduler_running === false && (
          <span className="pill border-status-danger-border bg-status-danger-bg text-status-danger-text">
            <AlertCircle className="h-3 w-3" aria-hidden="true" />
            scheduler stopped
          </span>
        )}
      </div>

      <ul className="flex flex-col gap-2">
        {retries.slice(0, 5).map((retry) => {
          const due = formatDue(retry.seconds_until_due)
          const isPending = retry.status === 'pending'

          return (
            <li
              key={retry.retry_id}
              className="flex flex-col gap-1.5 rounded-md border border-surface-3 bg-surface-1 p-2.5"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-mono text-[11px] text-text-primary">
                  {retry.event_id}
                </span>
                <span
                  className={`pill ${STATUS_STYLES[retry.status] ?? STATUS_STYLES.cancelled}`}
                >
                  {retry.status}
                </span>
              </div>

              <div className="tabular flex items-center justify-between gap-2 text-[10px] text-text-tertiary">
                <span>attempt {retry.attempt_number}</span>
                {due && (
                  <span className={retry.overdue ? 'text-status-warning-text' : ''}>
                    {retry.overdue ? 'overdue' : `in ${due}`}
                  </span>
                )}
              </div>

              {isPending && (
                <div className="flex gap-1.5">
                  {/* Bank-uptime delays are 20-45 minutes, so a demo needs a
                      way to exercise the retry path without waiting one out. */}
                  <button
                    onClick={() => runRetryNow(retry.retry_id)}
                    className="btn-ghost flex-1 justify-center"
                  >
                    <Play className="h-3 w-3" aria-hidden="true" />
                    Run now
                  </button>
                  <button
                    onClick={() => cancelRetry(retry.retry_id)}
                    className="btn-ghost flex-1 justify-center"
                  >
                    <X className="h-3 w-3" aria-hidden="true" />
                    Cancel
                  </button>
                </div>
              )}

              {retry.last_error && (
                <p className="text-[10px] leading-snug text-status-danger-text">
                  {retry.last_error.slice(0, 120)}
                </p>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
