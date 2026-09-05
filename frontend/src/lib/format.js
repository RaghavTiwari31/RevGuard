/**
 * Shared formatting + display vocabulary.
 *
 * Every rupee figure in the dashboard goes through here so the table, the
 * metric tiles and the drawer never disagree about how ₹3000 should look.
 */

const inrCompact = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  maximumFractionDigits: 0,
})

const inrPrecise = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

/** ₹3,000 — whole rupees, Indian digit grouping. */
export const formatINR = (value) => inrCompact.format(Number(value) || 0)

/** ₹0.40 — for per-message dispatch costs, where paise matter. */
export const formatINRPrecise = (value) => inrPrecise.format(Number(value) || 0)

/** +₹1,200 / −₹300 — always carries an explicit sign. */
export const formatDelta = (value) => {
  const n = Number(value) || 0
  const sign = n < 0 ? '−' : '+'
  return `${sign}${formatINR(Math.abs(n))}`
}

export const formatPct = (value, digits = 1) =>
  `${(Number(value) || 0).toFixed(digits)}%`

/** SCREAMING_SNAKE_CASE → Title Case, for enum values shown to humans. */
export const humanize = (value) =>
  typeof value === 'string' && value.length
    ? value
        .toLowerCase()
        .split('_')
        .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
        .join(' ')
    : '—'

export const formatTime = (iso) => {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? '—'
    : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export const formatClock = (iso) => {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? ''
    : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/**
 * Failure categories, with the colour treatment each one carries everywhere.
 * Keyed by the exact enum value the backend emits (app/classifier.py).
 */
export const CATEGORY_STYLES = {
  TRANSIENT_DOWNTIME: {
    label: 'Transient Downtime',
    className: 'text-status-info-text bg-status-info-bg border-status-info-border',
  },
  TEMPORARY_CASHFLOW: {
    label: 'Temporary Cashflow',
    className: 'text-status-warning-text bg-status-warning-bg border-status-warning-border',
  },
  EXPIRED_MANDATE: {
    label: 'Expired Mandate',
    className: 'text-brand-light bg-brand-500/15 border-brand-500/30',
  },
  DISPUTE_OR_OPTOUT: {
    label: 'Dispute / Opt-out',
    className: 'text-orange-300 bg-orange-950/50 border-orange-900/60',
  },
  UNRECOVERABLE_FRAUD: {
    label: 'Unrecoverable Fraud',
    className: 'text-purple-300 bg-purple-950/50 border-purple-900/60',
  },
}

export const categoryStyle = (category) =>
  CATEGORY_STYLES[category] ?? {
    label: humanize(category),
    className: 'text-text-secondary bg-status-neutral-bg border-status-neutral-border',
  }

/** Recovery actions (app/strategies/dispatcher.py::ActionType). */
export const ACTION_LABELS = {
  SCHEDULE_RETRY: 'Silent Retry',
  GENERATE_PAYMENT_LINK: 'Payment Link',
  SEND_MANDATE_LINK: 'Mandate Renewal',
  ESCALATED_HUMAN_ATTENTION: 'Escalated',
  DROPPED_NO_ACTION: 'Dropped',
}

export const actionLabel = (action) => ACTION_LABELS[action] ?? humanize(action)

/** Actions where no automated outreach is sent to the customer. */
export const NO_OUTREACH_ACTIONS = new Set([
  'ESCALATED_HUMAN_ATTENTION',
  'DROPPED_NO_ACTION',
])

export const isFrozen = (action) => NO_OUTREACH_ACTIONS.has(action)

/** Outcome statuses (app/strategies/dispatcher.py::OutcomeStatus). */
export const OUTCOME_STYLES = {
  RETRY_SCHEDULED: { label: 'Retry Scheduled', tone: 'info' },
  AWAITING_CUSTOMER_SETTLEMENT: { label: 'Awaiting Settlement', tone: 'warning' },
  AWAITING_MANDATE_RENEWAL: { label: 'Awaiting Renewal', tone: 'warning' },
  EXTENDED_BACKOFF: { label: 'Extended Backoff', tone: 'neutral' },
  ESCALATED: { label: 'Escalated', tone: 'danger' },
  DROPPED: { label: 'Dropped', tone: 'danger' },
}

export const TONE_CLASSES = {
  success: 'text-status-success-text bg-status-success-bg border-status-success-border',
  warning: 'text-status-warning-text bg-status-warning-bg border-status-warning-border',
  danger: 'text-status-danger-text bg-status-danger-bg border-status-danger-border',
  info: 'text-status-info-text bg-status-info-bg border-status-info-border',
  neutral: 'text-status-neutral-text bg-status-neutral-bg border-status-neutral-border',
}

export const outcomeStyle = (outcome) => {
  const { label, tone } = OUTCOME_STYLES[outcome] ?? {
    label: humanize(outcome),
    tone: 'neutral',
  }
  return { label, className: TONE_CLASSES[tone] }
}

export const CHANNEL_LABELS = {
  sms: 'SMS',
  whatsapp: 'WhatsApp',
  voice: 'Voice',
  none: 'None',
}

export const channelLabel = (channel) => CHANNEL_LABELS[channel] ?? humanize(channel)

export const CHANNEL_COLORS = {
  whatsapp: '#25d366',
  sms: '#38bdf8',
  voice: '#a78bfa',
}
