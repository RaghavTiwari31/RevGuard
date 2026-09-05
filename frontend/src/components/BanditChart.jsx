import React, { useMemo, useState } from 'react'
import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import { CHANNEL_COLORS, categoryStyle, channelLabel } from '../lib/format'

const CHANNEL_ORDER = ['whatsapp', 'sms', 'voice']

function ChartTooltip({ active, payload }) {
  if (!active || !payload?.length) return null
  const { name, reward, selections, pulls } = payload[0].payload
  return (
    <div className="rounded-md border border-surface-4 bg-surface-1 px-2.5 py-2 text-2xs shadow-raised">
      <div className="mb-1 font-semibold text-text-primary">{name}</div>
      <div className="tabular text-text-secondary">
        Mean reward {(reward * 100).toFixed(1)}%
      </div>
      <div className="tabular text-text-tertiary">
        {selections} selected · {pulls} scored
      </div>
    </div>
  )
}

export default function BanditChart({ stats }) {
  // Default to the busiest segment rather than hardcoding one that may have no
  // traffic in a given run.
  const segments = useMemo(
    () =>
      Object.keys(stats).sort(
        (a, b) => totalSelections(stats[b]) - totalSelections(stats[a]),
      ),
    [stats],
  )
  const [segment, setSegment] = useState(null)
  const active = segment && stats[segment] ? segment : segments[0]

  const data = useMemo(() => {
    const arms = stats[active] ?? {}
    return CHANNEL_ORDER.filter((c) => arms[c]).map((c) => ({
      key: c,
      name: channelLabel(c),
      reward: arms[c].mean_reward ?? 0,
      selections: arms[c].selections ?? 0,
      pulls: arms[c].pulls ?? 0,
      color: CHANNEL_COLORS[c],
    }))
  }, [stats, active])

  if (!active || data.length === 0) {
    return (
      <p className="py-3 text-center text-xs text-text-tertiary">
        No channel data for this run.
      </p>
    )
  }

  const totalSel = data.reduce((sum, d) => sum + d.selections, 0)

  return (
    <div className="flex flex-col gap-2">
      {segments.length > 1 && (
        <select
          value={active}
          onChange={(e) => setSegment(e.target.value)}
          className="field w-full"
          aria-label="Bandit segment"
        >
          {segments.map((s) => (
            <option key={s} value={s}>
              {categoryStyle(s).label}
            </option>
          ))}
        </select>
      )}

      <div className="h-[132px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={data}
            layout="vertical"
            margin={{ top: 4, right: 8, left: 0, bottom: 0 }}
          >
            <XAxis type="number" domain={[0, 1]} hide />
            <YAxis
              dataKey="name"
              type="category"
              width={62}
              tick={{ fontSize: 11, fill: '#a2a8b8' }}
              axisLine={false}
              tickLine={false}
            />
            <Tooltip content={<ChartTooltip />} cursor={{ fill: 'rgba(255,255,255,0.04)' }} />
            <Bar dataKey="reward" radius={[0, 4, 4, 0]} barSize={14}>
              {data.map((entry) => (
                <Cell key={entry.key} fill={entry.color} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      <p className="tabular text-center text-2xs text-text-tertiary">
        Mean reward per channel · {totalSel} selections
      </p>
    </div>
  )
}

function totalSelections(arms) {
  return Object.values(arms ?? {}).reduce((sum, a) => sum + (a.selections ?? 0), 0)
}
