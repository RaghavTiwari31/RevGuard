import React from 'react'
import { Trophy } from 'lucide-react'

import { formatINR, formatPct } from '../lib/format'

const ARMS = [
  { key: 'bandit', label: 'Adaptive bandit' },
  { key: 'deterministic', label: 'Cost-aware rule' },
]

/**
 * Head-to-head result of running both channel-selection strategies over
 * byte-identical data.
 *
 * Deliberately reports whichever strategy actually won, including when that is
 * the simple rule — a comparison that can only confirm the fancier option is
 * not a comparison.
 */
export default function AbComparison({ result }) {
  if (!result?.arms) return null

  const { arms, delta, verdict, mode, measures } = result
  const banditWon = verdict === 'bandit'

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-col gap-0.5">
          <span className="label">Winner</span>
          <span className="flex items-center gap-1.5 text-sm font-semibold text-text-primary">
            <Trophy className="h-3.5 w-3.5 text-brand-400" aria-hidden="true" />
            {verdict === 'tie'
              ? 'Tie'
              : ARMS.find((a) => a.key === verdict)?.label ?? verdict}
          </span>
        </div>
        <span className="pill border-surface-3 bg-surface-1 uppercase text-text-tertiary">
          {mode}
        </span>
      </div>

      {measures && (
        <p className="text-2xs leading-snug text-text-tertiary">{measures}</p>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-left text-2xs">
          <thead className="text-text-tertiary">
            <tr>
              <th className="pb-1.5 pr-2 font-semibold">Strategy</th>
              <th className="pb-1.5 px-2 text-right font-semibold">Recovered</th>
              <th className="pb-1.5 px-2 text-right font-semibold">Cost</th>
              <th className="pb-1.5 pl-2 text-right font-semibold">Yield</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-3/60">
            {ARMS.map(({ key, label }) => {
              const arm = arms[key]
              if (!arm) return null
              const won = verdict === key
              return (
                <tr key={key} className={won ? 'text-text-primary' : 'text-text-secondary'}>
                  <td className="py-1.5 pr-2">
                    <span className="flex items-center gap-1">
                      {won && (
                        <span
                          className="h-1 w-1 rounded-full bg-brand-400"
                          aria-hidden="true"
                        />
                      )}
                      {label}
                    </span>
                  </td>
                  <td className="tabular py-1.5 px-2 text-right">
                    {formatINR(arm.revguard_recovered_inr)}
                  </td>
                  <td className="tabular py-1.5 px-2 text-right">
                    {formatINR(arm.total_cost_inr)}
                  </td>
                  <td className="tabular py-1.5 pl-2 text-right">
                    {formatPct(arm.revguard_yield_pct)}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="flex items-baseline justify-between gap-2 border-t border-surface-3 pt-2.5">
        <span className="text-2xs text-text-tertiary">Bandit net vs rule</span>
        <span
          className={`tabular text-xs font-semibold ${
            banditWon ? 'text-status-success-text' : 'text-status-danger-text'
          }`}
        >
          {delta.net_recovery_inr >= 0 ? '+' : '−'}
          {formatINR(Math.abs(delta.net_recovery_inr))}
        </span>
      </div>
    </div>
  )
}
