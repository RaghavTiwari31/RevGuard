import React, { useEffect, useRef } from 'react'
import { ExternalLink, ShieldAlert, ShieldCheck, X } from 'lucide-react'

import { useStore } from '../store/useStore'
import {
  actionLabel,
  categoryStyle,
  channelLabel,
  formatClock,
  formatINR,
  formatINRPrecise,
  humanize,
  isFrozen,
  outcomeStyle,
} from '../lib/format'

function Section({ title, aside, children }) {
  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h4 className="label">{title}</h4>
        {aside}
      </div>
      {children}
    </section>
  )
}

function GuardCheck({ label, passed, failLabel = 'FAIL' }) {
  return (
    <div className="flex flex-col gap-1 rounded-md border border-surface-3 bg-surface-2 p-2.5">
      <span className="text-2xs uppercase tracking-label text-text-tertiary">{label}</span>
      <span
        className={`flex items-center gap-1 text-xs font-semibold ${
          passed ? 'text-status-success-text' : 'text-status-danger-text'
        }`}
      >
        {passed ? (
          <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
        ) : (
          <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
        )}
        {passed ? 'PASS' : failLabel}
      </span>
    </div>
  )
}

function DataRow({ label, children }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5">
      <span className="text-2xs uppercase tracking-label text-text-tertiary">{label}</span>
      <span className="tabular text-right text-xs text-text-primary">{children}</span>
    </div>
  )
}

export default function TransactionDetailsDrawer() {
  const { selectedEvent, setSelectedEvent } = useStore()
  const closeRef = useRef(null)

  const close = () => setSelectedEvent(null)

  // Escape closes the drawer, and focus moves into it on open so keyboard users
  // are not left behind in the table.
  useEffect(() => {
    if (!selectedEvent) return
    const onKeyDown = (e) => {
      if (e.key === 'Escape') close()
    }
    window.addEventListener('keydown', onKeyDown)
    closeRef.current?.focus()
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [selectedEvent])

  if (!selectedEvent) return null

  const ev = selectedEvent
  const category = categoryStyle(ev.category)
  const outcome = outcomeStyle(ev.outcome_status)
  const frozen = isFrozen(ev.action_type)
  const hasMessage = !!selectedEvent.hinglish_message
  const isDropped = selectedEvent.action_type === 'DROPPED_NO_ACTION' || selectedEvent.action_type === 'ESCALATED_HUMAN_ATTENTION'
  const isSilentRetry = selectedEvent.action_type === 'SCHEDULE_RETRY'
  const messageNotSent = isDropped || isSilentRetry

  // Guardrail results may be absent on live webhook events — absent means the
  // check did not report a failure, so default to pass rather than to alarm.
  const gc = ev.guardrail_checks || {}
  const confidence = typeof ev.confidence === 'number' ? ev.confidence : null

  return (
    <>
      <div
        className="absolute inset-0 z-10 animate-fade-in bg-black/60 backdrop-blur-[2px]"
        onClick={close}
        aria-hidden="true"
      />

      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Transaction details"
        className="absolute inset-y-0 right-0 z-20 flex w-full max-w-[460px] animate-slide-in flex-col border-l border-surface-3 bg-surface-1 shadow-drawer"
      >
        <header className="flex items-start justify-between gap-3 border-b border-surface-3 bg-surface-2 px-4 py-3.5">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="text-[13px] font-semibold text-text-primary">
                Transaction Details
              </h3>
              {ev.source === 'live_webhook' && (
                <span className="pill border-status-success-border bg-status-success-bg text-status-success-text">
                  LIVE
                </span>
              )}
            </div>
            <p className="mt-0.5 truncate font-mono text-2xs text-text-tertiary">
              {ev.trace_id || '—'}
            </p>
          </div>
          <button
            ref={closeRef}
            onClick={close}
            className="btn-ghost shrink-0 rounded-md p-1.5"
            aria-label="Close details"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </header>

        <div className="scrollbar-thin flex flex-1 flex-col gap-6 overflow-y-auto p-5">
          {/* Headline: what happened and what we did about it. */}
          <div className="flex flex-col gap-3 rounded-card border border-surface-3 bg-surface-2 p-4">
            <div className="flex items-center justify-between gap-2">
              <span className={`pill ${category.className}`}>{category.label}</span>
              <span className={`pill ${outcome.className}`}>{outcome.label}</span>
            </div>
            <div className="tabular text-2xl font-semibold leading-none tracking-[-0.02em] text-text-primary">
              {formatINR(ev.amount_inr)}
            </div>
            <div className="divide-y divide-surface-3/70 border-t border-surface-3 pt-1">
              <DataRow label="Action">{actionLabel(ev.action_type)}</DataRow>
              <DataRow label="Channel">
                {ev.dispatch_channel && ev.dispatch_channel !== 'none'
                  ? channelLabel(ev.dispatch_channel)
                  : '—'}
              </DataRow>
              <DataRow label="Dispatch Cost">
                {typeof ev.dispatch_cost_inr === 'number'
                  ? formatINRPrecise(ev.dispatch_cost_inr)
                  : '—'}
              </DataRow>
            </div>
          </div>

          <Section
            title="LLM Rationale"
            aside={
              confidence !== null && (
                <span
                  className={`pill ${
                    confidence >= 0.75
                      ? 'border-status-success-border bg-status-success-bg text-status-success-text'
                      : 'border-status-warning-border bg-status-warning-bg text-status-warning-text'
                  }`}
                  title="LLM confidence in the deterministic classification"
                >
                  <span className="tabular">{confidence.toFixed(2)}</span> confidence
                </span>
              )
            }
          >
            <blockquote className="rounded-md border border-surface-3 border-l-2 border-l-brand-500 bg-surface-2 p-3 text-[13px] leading-relaxed text-text-primary">
              {ev.rationale || 'No rationale available.'}
            </blockquote>

            <div className="flex flex-wrap items-center gap-1.5">
              {ev.classification_rule && (
                <span
                  className="pill border-surface-3 bg-surface-2 font-mono text-text-tertiary"
                  title="Deterministic rule that decided the category — the LLM never picks it"
                >
                  {ev.classification_rule}
                </span>
              )}
              {ev.provider_used && (
                <span className="pill border-surface-3 bg-surface-2 font-mono text-text-tertiary">
                  {ev.provider_used}
                </span>
              )}
            </div>
          </Section>

          <Section title="Guardrail Checks">
            <div className="grid grid-cols-2 gap-2.5">
              <GuardCheck
                label="Idempotency"
                passed={gc.idempotency_passed !== false}
                failLabel="DUPLICATE"
              />
              <GuardCheck
                label="Retry Cap"
                passed={gc.retry_cap_passed !== false}
                failLabel="EXCEEDED"
              />
              <GuardCheck
                label="Quiet Hours"
                passed={gc.quiet_hours_passed !== false}
                failLabel="BLOCKED"
              />
              <GuardCheck
                label="Anti-Spam"
                passed={gc.anti_spam_passed !== false}
                failLabel="COOLDOWN"
              />
            </div>
          </Section>

          <Section title="Outreach Preview">
            <div className="flex flex-col overflow-hidden rounded-card border border-surface-3 bg-surface-2">
              <div className="flex items-center gap-2 border-b border-surface-3 bg-surface-3/40 px-3 py-2">
                <div className="flex gap-1.5" aria-hidden="true">
                  <span className="h-2.5 w-2.5 rounded-full bg-surface-4" />
                  <span className="h-2.5 w-2.5 rounded-full bg-surface-4" />
                  <span className="h-2.5 w-2.5 rounded-full bg-surface-4" />
                </div>
                <span className="ml-1 font-mono text-2xs text-text-tertiary">
                  {ev.dispatch_channel && ev.dispatch_channel !== 'none'
                    ? `${channelLabel(ev.dispatch_channel).toLowerCase()}-preview`
                    : 'customer-message-preview'}
                </span>
              </div>

              <div className="min-h-[168px] bg-[#e9edef] p-4">
                {!hasMessage || messageNotSent ? (
                  <p className="mt-10 text-center text-xs italic text-gray-500">
                    {isSilentRetry ? "[Silent Retry — No automated message dispatched]" : "No automated message dispatched — automation frozen for this record."}
                  </p>
                ) : (
                  <div className="max-w-[92%] rounded-lg rounded-tl-sm border border-black/5 bg-white p-3 text-[13px] leading-relaxed text-gray-800 shadow-sm">
                    <p className="whitespace-pre-wrap">{ev.hinglish_message}</p>

                    {ev.razorpay_link_url && (
                      <a
                        href={ev.razorpay_link_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="mt-3 flex items-center gap-1.5 border-t border-gray-100 pt-2.5 text-xs font-semibold text-blue-600 hover:underline"
                      >
                        <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                        Pay {formatINR(ev.amount_inr)} securely
                      </a>
                    )}

                    <div className="mt-1.5 text-right text-[10px] text-gray-400">
                      {formatClock(ev.timestamp)}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </Section>

          <Section title="Audit Trail">
            <dl className="divide-y divide-surface-3/70 rounded-md border border-surface-3 bg-surface-2 px-3">
              <DataRow label="Event ID">
                <span className="font-mono">{ev.event_id || '—'}</span>
              </DataRow>
              <DataRow label="Outcome">{humanize(ev.outcome_status)}</DataRow>
              <DataRow label="Processed">{formatClock(ev.timestamp) || '—'}</DataRow>
            </dl>
          </Section>
        </div>
      </aside>
    </>
  )
}
