import React from 'react'
import {
  Activity,
  ChevronRight,
  ExternalLink,
  Link2,
  MessageSquare,
  PhoneCall,
  RefreshCcw,
  ShieldAlert,
  Smartphone,
} from 'lucide-react'

import { useStore } from '../store/useStore'
import {
  actionLabel,
  categoryStyle,
  channelLabel,
  formatINR,
  formatTime,
  isFrozen,
  outcomeStyle,
} from '../lib/format'

const ACTION_ICONS = {
  SCHEDULE_RETRY: Activity,
  GENERATE_PAYMENT_LINK: Link2,
  SEND_MANDATE_LINK: RefreshCcw,
  ESCALATED_HUMAN_ATTENTION: ShieldAlert,
  DROPPED_NO_ACTION: ShieldAlert,
}

const CHANNEL_ICONS = {
  whatsapp: MessageSquare,
  sms: Smartphone,
  voice: PhoneCall,
}

const CHANNEL_CLASSES = {
  whatsapp: 'text-channel-whatsapp',
  sms: 'text-channel-sms',
  voice: 'text-channel-voice',
}

/** Short, readable handle for a payment id — the tail is the distinguishing part. */
const shortId = (eventId) => {
  if (typeof eventId !== 'string' || !eventId) return '—'
  const tail = eventId.split('_').pop()
  return tail && tail.length > 4 ? tail.slice(0, 12) : eventId
}

function EmptyState() {
  return (
    <div className="panel flex min-h-[420px] flex-1 flex-col items-center justify-center gap-1 p-8 text-center">
      <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-full border border-surface-3 bg-surface-1">
        <Activity className="h-5 w-5 text-text-tertiary" aria-hidden="true" />
      </div>
      <p className="text-sm font-medium text-text-secondary">No transactions yet</p>
      <p className="max-w-xs text-xs text-text-tertiary">
        Start a batch from the sidebar, or POST a Razorpay webhook to{' '}
        <code className="rounded bg-surface-1 px-1 py-0.5 font-mono text-2xs text-text-secondary">
          /webhook
        </code>
        .
      </p>
    </div>
  )
}

function Row({ event, isSelected, onSelect }) {
  const category = categoryStyle(event.category)
  const outcome = outcomeStyle(event.outcome_status)
  const frozen = isFrozen(event.action_type)
  const ActionIcon = ACTION_ICONS[event.action_type] ?? ChevronRight
  const ChannelIcon = CHANNEL_ICONS[event.dispatch_channel]

  return (
    <tr
      onClick={() => onSelect(event)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onSelect(event)
        }
      }}
      tabIndex={0}
      role="button"
      aria-label={`Transaction ${shortId(event.event_id)}, ${category.label}`}
      className={`group animate-row-in cursor-pointer transition-colors ${
        isSelected ? 'bg-brand-500/10' : 'hover:bg-surface-3/40'
      }`}
    >
      {/* Selected-row marker, drawn inside the first cell so it never shifts layout. */}
      <td className="relative whitespace-nowrap py-2.5 pl-4 pr-3">
        {isSelected && (
          <span className="absolute inset-y-0 left-0 w-0.5 bg-brand-500" aria-hidden="true" />
        )}
        <div className="flex flex-col leading-tight">
          <span className="font-mono text-[11px] text-text-primary">
            {shortId(event.event_id)}
          </span>
          <span className="mt-0.5 text-2xs text-text-tertiary">
            {formatTime(event.timestamp)}
          </span>
        </div>
      </td>

      <td className="whitespace-nowrap px-3 py-2.5 text-right font-medium text-text-primary">
        {formatINR(event.amount_inr)}
      </td>

      <td className="px-3 py-2.5">
        <span className={`pill ${category.className}`}>{category.label}</span>
      </td>

      <td className="whitespace-nowrap px-3 py-2.5">
        <span className="flex items-center gap-1.5 font-medium text-text-primary">
          <ActionIcon
            className={`h-3.5 w-3.5 shrink-0 ${
              frozen ? 'text-status-danger-text' : 'text-text-tertiary'
            }`}
            aria-hidden="true"
          />
          {actionLabel(event.action_type)}
        </span>
      </td>

      <td className="px-3 py-2.5">
        {event.dispatch_channel && event.dispatch_channel !== 'none' && !frozen ? (
          <span className="pill border-surface-3 bg-surface-1 text-text-secondary">
            {ChannelIcon && (
              <ChannelIcon
                className={`h-3 w-3 ${CHANNEL_CLASSES[event.dispatch_channel] ?? ''}`}
                aria-hidden="true"
              />
            )}
            {channelLabel(event.dispatch_channel)}
          </span>
        ) : (
          <span className="text-text-tertiary" aria-label="No outreach">
            —
          </span>
        )}
      </td>

      <td className="px-3 py-2.5">
        <span className={`pill ${outcome.className}`}>{outcome.label}</span>
      </td>

      <td className="w-8 py-2.5 pr-4 text-right">
        <ChevronRight
          className={`ml-auto h-4 w-4 transition-opacity ${
            isSelected
              ? 'text-brand-400 opacity-100'
              : 'text-text-tertiary opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100'
          }`}
          aria-hidden="true"
        />
      </td>
    </tr>
  )
}

export default function TransactionTable() {
  const { events, selectedEvent, setSelectedEvent, isRunning } = useStore()

  if (events.length === 0) return <EmptyState />

  return (
    <div className="panel flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="panel-header">
        <h2 className="text-[13px] font-semibold tracking-[-0.01em] text-text-primary">
          Live Transactions
        </h2>
        <span className="pill border-surface-3 bg-surface-1 text-text-tertiary">
          {isRunning && (
            <span
              className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand-400"
              aria-hidden="true"
            />
          )}
          <span className="tabular">{events.length}</span> events
        </span>
      </div>

      <div className="scrollbar-thin min-h-0 flex-1 overflow-auto">
        <table className="w-full border-collapse text-left text-xs text-text-secondary">
          <thead className="sticky top-0 z-10 bg-surface-2 text-2xs uppercase tracking-label text-text-tertiary shadow-[0_1px_0_0_theme(colors.surface.3)]">
            <tr>
              <th scope="col" className="py-2.5 pl-4 pr-3 font-semibold">
                Payment / Time
              </th>
              <th scope="col" className="px-3 py-2.5 text-right font-semibold">
                Amount
              </th>
              <th scope="col" className="px-3 py-2.5 font-semibold">
                Category
              </th>
              <th scope="col" className="px-3 py-2.5 font-semibold">
                Action
              </th>
              <th scope="col" className="px-3 py-2.5 font-semibold">
                Channel
              </th>
              <th scope="col" className="px-3 py-2.5 font-semibold">
                Outcome
              </th>
              <th scope="col" className="w-8 pr-4">
                <span className="sr-only">Open details</span>
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-3/60">
            {events.map((event) => (
              <Row
                key={event.trace_id ?? event.event_id}
                event={event}
                isSelected={selectedEvent?.trace_id === event.trace_id}
                onSelect={setSelectedEvent}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
