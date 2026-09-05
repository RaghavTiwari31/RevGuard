import React from 'react'
import { useStore } from '../store/useStore'
import { ShieldAlert, CheckCircle2, ChevronRight, Activity, ArrowRightCircle, ExternalLink, MessageSquare, PhoneCall, Smartphone } from 'lucide-react'

const catColors = {
  TRANSIENT_DOWNTIME: 'text-status-info-text bg-status-info-bg border-status-info-border',
  TEMPORARY_CASHFLOW: 'text-status-warning-text bg-status-warning-bg border-status-warning-border',
  EXPIRED_MANDATE: 'text-brand-light bg-brand/20 border-brand/30',
  ESCALATED_HUMAN_ATTENTION: 'text-status-danger-text bg-status-danger-bg border-status-danger-border',
  UNRECOVERABLE_FRAUD: 'text-purple-400 bg-purple-900/30 border-purple-800/50',
  DISPUTE_OR_OPTOUT: 'text-orange-400 bg-orange-900/30 border-orange-800/50',
  CIRCUIT_BREAKER: 'text-status-danger-text bg-status-danger-bg border-status-danger-border'
}

function ActionIcon({ type }) {
  switch (type) {
    case 'SCHEDULE_RETRY': return <Activity className="w-3.5 h-3.5" />;
    case 'GENERATE_PAYMENT_LINK': return <ExternalLink className="w-3.5 h-3.5" />;
    case 'SEND_MANDATE_LINK': return <RefreshCcw className="w-3.5 h-3.5" />;
    case 'ESCALATED_HUMAN_ATTENTION': return <ShieldAlert className="w-3.5 h-3.5" />;
    case 'DROPPED_NO_ACTION': return <ShieldAlert className="w-3.5 h-3.5" />;
    default: return <ArrowRightCircle className="w-3.5 h-3.5" />;
  }
}

function RefreshCcw(props) { return <svg {...props} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg> }

export default function TransactionTable() {
  const { events, setSelectedEvent, selectedEvent } = useStore()

  if (events.length === 0) {
    return (
      <div className="panel flex-1 flex flex-col items-center justify-center text-text-tertiary p-8 text-center min-h-[400px]">
        <Activity className="w-8 h-8 mb-3 opacity-20" />
        <p className="text-sm">No transactions to display.</p>
        <p className="text-xs mt-1 opacity-70">Start a simulation batch from the sidebar.</p>
      </div>
    )
  }

  return (
    <div className="panel flex-1 flex flex-col overflow-hidden">
      <div className="panel-header flex justify-between items-center">
        <h2 className="text-sm font-semibold tracking-tight">Recent Transactions</h2>
        <span className="text-xs text-text-tertiary">{events.length} events</span>
      </div>
      
      <div className="flex-1 overflow-auto scrollbar-hide">
        <table className="w-full text-left border-collapse">
          <thead className="sticky top-0 bg-surface-2 border-b border-surface-3 text-[10px] uppercase tracking-wider text-text-tertiary">
            <tr>
              <th className="py-2.5 px-4 font-medium">Time / ID</th>
              <th className="py-2.5 px-4 font-medium text-right">Amount</th>
              <th className="py-2.5 px-4 font-medium">Category</th>
              <th className="py-2.5 px-4 font-medium">Action Taken</th>
              <th className="py-2.5 px-4 font-medium">Channel</th>
              <th className="py-2.5 px-4 font-medium text-right pr-6">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-3/50 text-xs text-text-secondary">
            {events.map((ev) => {
              const isSelected = selectedEvent?.trace_id === ev.trace_id;
              const isDropped = ev.action_type === 'DROPPED_NO_ACTION' || ev.action_type === 'ESCALATED_HUMAN_ATTENTION';
              
              return (
                <tr 
                  key={ev.trace_id}
                  onClick={() => setSelectedEvent(ev)}
                  className={`
                    cursor-pointer transition-colors group
                    ${isSelected ? 'bg-surface-3/50' : 'hover:bg-surface-3/30'}
                  `}
                >
                  <td className="py-3 px-4 whitespace-nowrap">
                    <div className="flex flex-col">
                      <span className="text-text-primary font-mono">{ev.event_id.split('_').pop()}</span>
                      <span className="text-[10px] text-text-tertiary mt-0.5">{new Date(ev.timestamp).toLocaleTimeString()}</span>
                    </div>
                  </td>
                  <td className="py-3 px-4 font-mono text-text-primary text-right whitespace-nowrap">
                    ₹{ev.amount_inr}
                  </td>
                  <td className="py-3 px-4">
                    <span className={`inline-flex px-2 py-0.5 rounded border text-[10px] font-medium tracking-wide ${catColors[ev.category] || 'text-gray-400 border-gray-700'}`}>
                      {ev.category.replace(/_/g, ' ')}
                    </span>
                  </td>
                  <td className="py-3 px-4">
                    <div className="flex items-center gap-1.5 text-text-primary font-medium">
                      <ActionIcon type={ev.action_type} />
                      {ev.action_type.replace(/_/g, ' ')}
                    </div>
                  </td>
                  <td className="py-3 px-4">
                    {ev.dispatch_channel && !isDropped ? (
                      <span className="inline-flex items-center gap-1 text-[10px] uppercase text-text-secondary bg-surface-3 px-1.5 py-0.5 rounded">
                        {ev.dispatch_channel === 'whatsapp' ? <MessageSquare className="w-3 h-3" /> :
                         ev.dispatch_channel === 'sms' ? <Smartphone className="w-3 h-3" /> :
                         <PhoneCall className="w-3 h-3" />}
                        {ev.dispatch_channel}
                      </span>
                    ) : (
                      <span className="text-text-tertiary">-</span>
                    )}
                  </td>
                  <td className="py-3 px-4 pr-6 text-right relative">
                    <div className="flex justify-end items-center gap-2">
                      {isDropped ? (
                        <ShieldAlert className="w-4 h-4 text-status-danger-text" />
                      ) : (
                        <CheckCircle2 className="w-4 h-4 text-status-success-text" />
                      )}
                    </div>
                    {/* Hover indicator for clickability */}
                    <div className={`absolute right-2 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 transition-opacity ${isSelected ? 'opacity-100 text-brand' : 'text-text-tertiary'}`}>
                      <ChevronRight className="w-4 h-4" />
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
