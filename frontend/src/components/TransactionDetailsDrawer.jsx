import React from 'react'
import { useStore } from '../store/useStore'
import { X, ShieldCheck, ShieldAlert, MessageSquare, ExternalLink } from 'lucide-react'

export default function TransactionDetailsDrawer() {
  const { selectedEvent, setSelectedEvent } = useStore()
  
  if (!selectedEvent) return null;

  const hasMessage = !!selectedEvent.hinglish_message
  const isDropped = selectedEvent.action_type === 'DROPPED_NO_ACTION' || selectedEvent.action_type === 'ESCALATED_HUMAN_ATTENTION'
  
  // Safely resolve guardrail checks — may be absent on live webhook events
  const gc = selectedEvent.guardrail_checks || {}
  const idempotencyPassed = gc.idempotency_passed !== false  // default true if missing
  const retryCapPassed    = gc.retry_cap_passed    !== false
  const quietHoursPassed  = gc.quiet_hours_passed  !== false
  const antiSpamPassed    = gc.anti_spam_passed    !== false

  const confidence = typeof selectedEvent.confidence === 'number' ? selectedEvent.confidence : null

  return (
    <>
      {/* Backdrop overlay */}
      <div 
        className="absolute inset-0 bg-black/50 backdrop-blur-[1px] z-10 transition-opacity animate-fade-in"
        onClick={() => setSelectedEvent(null)}
      />
      
      {/* Drawer */}
      <div className="absolute top-0 right-0 bottom-0 w-[450px] bg-surface-1 border-l border-surface-3 shadow-2xl z-20 flex flex-col animate-slide-in">
        
        {/* Header */}
        <div className="p-4 border-b border-surface-3 flex justify-between items-center bg-surface-2">
          <div>
            <h3 className="text-sm font-semibold text-text-primary">Transaction Details</h3>
            <p className="text-[10px] text-text-tertiary font-mono mt-0.5">{selectedEvent.trace_id}</p>
          </div>
          <button 
            onClick={() => setSelectedEvent(null)}
            className="p-1.5 hover:bg-surface-3 rounded-md text-text-secondary transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-5 flex flex-col gap-6">
          
          {/* AI Decision Rationale */}
          <div className="flex flex-col gap-2">
            <h4 className="text-xs font-semibold text-text-secondary uppercase tracking-wider flex justify-between items-center">
              <div className="flex items-center gap-2">
                <span>LLM Rationale</span>
                {selectedEvent.classification_rule && (
                  <span className="text-[9px] uppercase tracking-wider bg-surface-3 px-1.5 py-0.5 rounded text-text-tertiary">
                    {selectedEvent.classification_rule.replace(/_/g, ' ')}
                  </span>
                )}
              </div>
              {confidence !== null && (
                <span className="text-brand">Conf: {confidence.toFixed(2)}</span>
              )}
            </h4>
            <div className="bg-surface-2 border border-surface-3 rounded-md p-3 text-sm text-text-primary italic leading-relaxed border-l-2 border-l-brand">
               "{selectedEvent.rationale || 'No rationale available.'}"
            </div>
          </div>

          {/* Guardrails */}
          <div className="flex flex-col gap-2">
            <h4 className="text-xs font-semibold text-text-secondary uppercase tracking-wider">Guardrail Checks</h4>
            <div className="grid grid-cols-2 gap-3">
               <GuardCheck label="Idempotency"  passed={idempotencyPassed} failLabel="DUPLICATE" />
               <GuardCheck label="Retry Cap"    passed={retryCapPassed}    failLabel="EXCEEDED" />
               <GuardCheck label="Quiet Hours"  passed={quietHoursPassed}  failLabel="BLOCKED" />
               <GuardCheck label="Anti-Spam"    passed={antiSpamPassed}    failLabel="COOLDOWN" />
            </div>
          </div>

          {/* Provider badge */}
          {selectedEvent.provider_used && (
            <div className="flex items-center gap-2">
              <span className="text-[10px] text-text-tertiary uppercase tracking-wider">LLM Provider</span>
              <span className="text-[10px] bg-surface-3 border border-surface-4 px-2 py-0.5 rounded font-mono text-text-secondary">
                {selectedEvent.provider_used}
              </span>
              {selectedEvent.source === 'live_webhook' && (
                <span className="text-[10px] bg-green-900/30 border border-green-800/50 text-green-400 px-2 py-0.5 rounded">
                  LIVE
                </span>
              )}
            </div>
          )}

          {/* Outreach Preview */}
          <div className="flex flex-col gap-2 flex-1">
            <h4 className="text-xs font-semibold text-text-secondary uppercase tracking-wider">Outreach Preview</h4>
            
            <div className="flex-1 bg-surface-2 border border-surface-3 rounded-lg flex flex-col overflow-hidden">
               {/* Emulator Header */}
               <div className="bg-surface-3/50 px-3 py-2 flex items-center gap-2 border-b border-surface-3">
                 <div className="flex gap-1.5">
                   <div className="w-2.5 h-2.5 rounded-full bg-surface-4"></div>
                   <div className="w-2.5 h-2.5 rounded-full bg-surface-4"></div>
                   <div className="w-2.5 h-2.5 rounded-full bg-surface-4"></div>
                 </div>
                 <span className="text-[10px] font-mono text-text-tertiary ml-2">customer-message-preview</span>
               </div>
               
               {/* Message Body */}
               <div className="flex-1 p-4 bg-[#f0f2f5] flex flex-col items-start justify-start">
                 {!hasMessage || isDropped ? (
                   <div className="w-full text-center text-xs text-gray-500 italic mt-10">
                     [No automated message dispatched]
                   </div>
                 ) : (
                   <div className="bg-white p-3 rounded-lg shadow-sm border border-gray-200 text-sm text-gray-800 max-w-[90%]">
                     <p className="whitespace-pre-wrap">{selectedEvent.hinglish_message}</p>
                     
                     {selectedEvent.razorpay_link_url && (
                       <div className="mt-3 pt-3 border-t border-gray-100 flex items-center gap-2 text-blue-600 font-medium text-xs">
                         <ExternalLink className="w-3.5 h-3.5" /> 
                         Pay ₹{selectedEvent.amount_inr} Securely
                       </div>
                     )}
                     
                     <div className="text-[9px] text-gray-400 text-right mt-1.5">
                       {selectedEvent.timestamp
                         ? new Date(selectedEvent.timestamp).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})
                         : ''}
                     </div>
                   </div>
                 )}
               </div>
            </div>
          </div>

        </div>
      </div>
    </>
  )
}

function GuardCheck({ label, passed, failLabel = 'FAIL' }) {
  return (
    <div className="bg-surface-2 border border-surface-3 rounded-md p-3 flex flex-col gap-1">
      <span className="text-[10px] text-text-tertiary uppercase">{label}</span>
      <span className={`text-xs font-semibold flex items-center gap-1 ${passed ? 'text-status-success-text' : 'text-status-danger-text'}`}>
        {passed ? <ShieldCheck className="w-3.5 h-3.5"/> : <ShieldAlert className="w-3.5 h-3.5"/>}
        {passed ? 'PASS' : failLabel}
      </span>
    </div>
  )
}
