import React from 'react'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'

export default function BanditChart({ stats }) {
  // Flatten stats for chart
  // We only care about TEMPORARY_CASHFLOW for the demo visualization as it has the most traffic
  const segment = 'TEMPORARY_CASHFLOW'
  const segmentStats = stats[segment]
  
  if (!segmentStats) return null;

  const data = [
    { name: 'WhatsApp', value: segmentStats.whatsapp?.mean_reward || 0, color: '#25D366' },
    { name: 'SMS', value: segmentStats.sms?.mean_reward || 0, color: '#3b82f6' },
    { name: 'Voice', value: segmentStats.voice?.mean_reward || 0, color: '#8b5cf6' }
  ]

  return (
    <div className="w-full h-[150px]">
      <div className="text-[10px] text-gray-500 mb-1 text-center">Mean Reward for {segment}</div>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 0, right: 10, left: -20, bottom: 0 }}>
          <XAxis type="number" domain={[0, 1]} hide />
          <YAxis dataKey="name" type="category" tick={{fontSize: 10, fill: '#9ca3af'}} axisLine={false} tickLine={false} />
          <Tooltip 
             contentStyle={{backgroundColor: '#1c2333', borderColor: '#374151', fontSize: '12px'}}
             itemStyle={{color: '#fff'}}
             formatter={(value) => [(value * 100).toFixed(1) + '%', 'Mean Reward']}
          />
          <Bar dataKey="value" radius={[0, 4, 4, 0]} barSize={16}>
            {data.map((entry, index) => (
              <Cell key={`cell-${index}`} fill={entry.color} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
