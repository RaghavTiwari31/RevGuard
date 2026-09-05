import React, { useState } from 'react'
import { useStore } from '../store/useStore'
import { Play, Settings2, SlidersHorizontal, RefreshCw } from 'lucide-react'
import BanditChart from './BanditChart'

export default function ControlPanel() {
  const { startSimulation, isRunning, policy, updatePolicy, summary } = useStore()
  const [seed, setSeed] = useState(42)
  const [isEditingPolicy, setIsEditingPolicy] = useState(false)
  const [localPolicy, setLocalPolicy] = useState(null)

  const handleStart = () => {
    if (!isRunning) startSimulation(seed)
  }

  const handleEditClick = () => {
    setLocalPolicy(policy)
    setIsEditingPolicy(true)
  }

  const handleSavePolicy = () => {
    updatePolicy({
      min_confidence_for_autonomous_action: Number(localPolicy.min_confidence_for_autonomous_action),
      voice_call_min_amount_inr: Number(localPolicy.voice_call_min_amount_inr)
    })
    setIsEditingPolicy(false)
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Simulation Controls */}
      <div className="flex flex-col gap-3">
        <h2 className="text-xs font-semibold text-text-secondary uppercase tracking-wider flex items-center gap-1.5">
          <Play className="w-3.5 h-3.5" /> Batch Runner
        </h2>
        
        <div className="panel p-3 flex flex-col gap-3">
          <div className="flex justify-between items-center text-xs">
             <span className="text-text-secondary">Dataset Size</span>
             <span className="font-mono text-text-primary">100 Records</span>
          </div>
          <div className="flex justify-between items-center text-xs">
             <span className="text-text-secondary">Random Seed</span>
             <input 
               type="number" 
               value={seed}
               onChange={(e) => setSeed(e.target.value)}
               disabled={isRunning}
               className="bg-surface-0 border border-surface-3 rounded px-2 py-1 w-16 text-right font-mono text-text-primary outline-none focus:border-brand disabled:opacity-50"
             />
          </div>
          
          <button 
            onClick={handleStart}
            disabled={isRunning}
            className={`
              mt-1 w-full py-2 rounded-md font-medium text-xs flex justify-center items-center gap-2 transition-colors
              ${isRunning 
                ? 'bg-surface-3 text-text-tertiary cursor-not-allowed border border-transparent' 
                : 'bg-text-primary hover:bg-white text-surface-0 border border-transparent shadow-sm'}
            `}
          >
            {isRunning ? (
              <><RefreshCw className="w-3.5 h-3.5 animate-spin" /> Running...</>
            ) : (
              <><Play className="w-3.5 h-3.5" /> Start Simulation</>
            )}
          </button>
        </div>
      </div>

      {/* Differentiator #6: Live Policy Editor */}
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h2 className="text-xs font-semibold text-text-secondary uppercase tracking-wider flex items-center gap-1.5">
            <Settings2 className="w-3.5 h-3.5" /> Policy Editor
          </h2>
          <button 
            onClick={isEditingPolicy ? handleSavePolicy : handleEditClick}
            className="text-[10px] bg-surface-3 hover:bg-surface-4 px-2 py-0.5 rounded text-text-secondary transition-colors"
          >
            {isEditingPolicy ? 'Save' : 'Edit'}
          </button>
        </div>
        
        <div className="panel p-3">
          {policy ? (
            <div className="flex flex-col gap-4 text-xs">
              <div className="flex flex-col gap-1.5">
                <label className="flex justify-between">
                  <span className="text-text-secondary">LLM Min Confidence</span>
                  <span className="font-mono text-text-primary">{isEditingPolicy ? localPolicy.min_confidence_for_autonomous_action : policy.min_confidence_for_autonomous_action}</span>
                </label>
                <input 
                  type="range" 
                  min="0.1" max="1.0" step="0.05"
                  disabled={!isEditingPolicy}
                  value={isEditingPolicy ? localPolicy.min_confidence_for_autonomous_action : policy.min_confidence_for_autonomous_action}
                  onChange={(e) => setLocalPolicy({...localPolicy, min_confidence_for_autonomous_action: e.target.value})}
                  className="w-full accent-brand disabled:opacity-50"
                />
                <p className="text-[10px] text-text-tertiary leading-tight">Below this, escalate to humans.</p>
              </div>
              
              <div className="flex flex-col gap-1.5 pt-3 border-t border-surface-3">
                <label className="flex justify-between">
                  <span className="text-text-secondary">Min Amount for Voice (₹)</span>
                  <span className="font-mono text-text-primary">{isEditingPolicy ? localPolicy.voice_call_min_amount_inr : policy.voice_call_min_amount_inr}</span>
                </label>
                <input 
                  type="range" 
                  min="0" max="5000" step="100"
                  disabled={!isEditingPolicy}
                  value={isEditingPolicy ? localPolicy.voice_call_min_amount_inr : policy.voice_call_min_amount_inr}
                  onChange={(e) => setLocalPolicy({...localPolicy, voice_call_min_amount_inr: e.target.value})}
                  className="w-full accent-brand disabled:opacity-50"
                />
              </div>
            </div>
          ) : (
            <div className="text-xs text-text-tertiary text-center py-2 animate-pulse">Loading policy...</div>
          )}
        </div>
      </div>

      {/* Differentiator #1: Adaptive Channel Bandit Stats */}
      <div className="flex flex-col gap-3">
        <h2 className="text-xs font-semibold text-text-secondary uppercase tracking-wider flex items-center gap-1.5">
          <SlidersHorizontal className="w-3.5 h-3.5" /> Channel Bandit
        </h2>
        <div className="panel p-3">
          {summary?.bandit_stats ? (
             <BanditChart stats={summary.bandit_stats} />
          ) : (
             <div className="text-xs text-text-tertiary text-center py-4">
               Learning inactive.
             </div>
          )}
        </div>
      </div>
    </div>
  )
}
