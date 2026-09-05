import React, { Suspense, lazy, useState } from 'react'
import {
  Check,
  GitCompareArrows,
  Landmark,
  Play,
  RefreshCw,
  RotateCcw,
  Settings2,
  SlidersHorizontal,
  Timer,
  X,
} from 'lucide-react'

import { useStore } from '../store/useStore'
import { formatINR } from '../lib/format'

// Recharts is the single largest dependency in the bundle and is only needed
// once a batch has produced learning data, so it is fetched on demand.
const BanditChart = lazy(() => import('./BanditChart'))

import AbComparison from './AbComparison'
import IssuerRadar from './IssuerRadar'
import RetryQueue from './RetryQueue'

function Panel({ icon: Icon, title, action, children }) {
  return (
    <section className="flex flex-col gap-2.5">
      <div className="flex items-center justify-between gap-2">
        <h2 className="label flex items-center gap-1.5">
          <Icon className="h-3.5 w-3.5" aria-hidden="true" />
          {title}
        </h2>
        {action}
      </div>
      <div className="panel p-3">{children}</div>
    </section>
  )
}

function Slider({ id, label, value, display, min, max, step, disabled, onChange, hint }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-text-secondary">{label}</span>
        <span className="tabular text-xs font-semibold text-text-primary">{display}</span>
      </label>
      <input
        id={id}
        type="range"
        className="range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      {hint && <p className="text-2xs leading-snug text-text-tertiary">{hint}</p>}
    </div>
  )
}

export default function ControlPanel() {
  const {
    startSimulation,
    startAbComparison,
    isRunning,
    policy,
    updatePolicy,
    resetPolicy,
    summary,
    issuers,
    retries,
    abResult,
  } = useStore()

  const [seed, setSeed] = useState(42)
  const [warmAb, setWarmAb] = useState(false)
  const [draft, setDraft] = useState(null) // Non-null while editing policy
  const [saving, setSaving] = useState(false)

  const isEditing = draft !== null

  const beginEdit = () =>
    setDraft({
      min_confidence_for_autonomous_action: Number(
        policy.min_confidence_for_autonomous_action,
      ),
      voice_call_min_amount_inr: Number(policy.voice_call_min_amount_inr),
      enable_adaptive_channel_bandit: Boolean(policy.enable_adaptive_channel_bandit),
    })

  const save = async () => {
    setSaving(true)
    // The store keeps the edit open if the backend rejects the patch, so the
    // user can correct the value instead of losing it.
    const ok = await updatePolicy(draft)
    setSaving(false)
    if (ok) setDraft(null)
  }

  return (
    <div className="flex flex-col gap-6">
      {/* ── Batch runner ─────────────────────────────────────────────────── */}
      <Panel icon={Play} title="Batch Runner">
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between text-xs">
            <span className="text-text-secondary">Dataset size</span>
            <span className="tabular font-medium text-text-primary">100 records</span>
          </div>

          <div className="flex items-center justify-between gap-2 text-xs">
            <label htmlFor="seed" className="text-text-secondary">
              Random seed
            </label>
            <input
              id="seed"
              type="number"
              className="field tabular w-20 text-right"
              value={seed}
              min={0}
              disabled={isRunning}
              // Coerce here so an emptied field cannot reach the API as "".
              onChange={(e) => setSeed(e.target.value === '' ? 0 : Number(e.target.value))}
            />
          </div>

          <button
            onClick={() => !isRunning && startSimulation(seed)}
            disabled={isRunning}
            className="btn-primary mt-0.5 w-full"
          >
            {isRunning ? (
              <>
                <RefreshCw className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                Running…
              </>
            ) : (
              <>
                <Play className="h-3.5 w-3.5" aria-hidden="true" />
                Start Simulation
              </>
            )}
          </button>

          <p className="text-2xs leading-snug text-text-tertiary">
            Replays synthetic failures through the same pipeline as live webhooks.
          </p>
        </div>
      </Panel>

      {/* ── A/B: bandit vs deterministic ─────────────────────────────────── */}
      <Panel icon={GitCompareArrows} title="Strategy A/B">
        <div className="flex flex-col gap-3">
          <p className="text-2xs leading-snug text-text-tertiary">
            Runs the batch under both channel-selection strategies over identical
            data, so the difference is attributable to selection alone.
          </p>

          <label className="flex items-center justify-between gap-2 text-xs">
            <span className="text-text-secondary">Pre-train the bandit</span>
            <input
              type="checkbox"
              checked={warmAb}
              disabled={isRunning}
              onChange={(e) => setWarmAb(e.target.checked)}
              className="h-3.5 w-3.5 accent-brand-500 disabled:opacity-50"
            />
          </label>
          <p className="-mt-1.5 text-[10px] leading-snug text-text-tertiary">
            {warmAb
              ? 'Steady state — what production sees once weights persist.'
              : 'Cold start — includes the price the bandit pays to explore.'}
          </p>

          <button
            onClick={() => !isRunning && startAbComparison(seed, warmAb)}
            disabled={isRunning}
            className="btn-secondary w-full"
          >
            <GitCompareArrows className="h-3.5 w-3.5" aria-hidden="true" />
            Compare Strategies
          </button>

          {abResult && (
            <div className="border-t border-surface-3 pt-3">
              <AbComparison result={abResult} />
            </div>
          )}
        </div>
      </Panel>

      {/* ── Issuer Health Radar ──────────────────────────────────────────── */}
      <Panel icon={Landmark} title="Issuer Health">
        <IssuerRadar data={issuers} />
      </Panel>

      {/* ── Durable retry queue ──────────────────────────────────────────── */}
      <Panel icon={Timer} title="Retry Queue">
        <RetryQueue data={retries} />
      </Panel>

      {/* ── Live policy editor ───────────────────────────────────────────── */}
      <Panel
        icon={Settings2}
        title="Policy Editor"
        action={
          policy && (
            <div className="flex items-center gap-1">
              {isEditing ? (
                <>
                  <button
                    onClick={() => setDraft(null)}
                    className="btn-ghost"
                    aria-label="Cancel policy edit"
                  >
                    <X className="h-3 w-3" aria-hidden="true" />
                    Cancel
                  </button>
                  <button onClick={save} disabled={saving} className="btn-ghost text-brand-light">
                    <Check className="h-3 w-3" aria-hidden="true" />
                    {saving ? 'Saving…' : 'Save'}
                  </button>
                </>
              ) : (
                <>
                  <button
                    onClick={resetPolicy}
                    className="btn-ghost"
                    title="Reload thresholds from policy.yaml"
                  >
                    <RotateCcw className="h-3 w-3" aria-hidden="true" />
                    Reset
                  </button>
                  <button onClick={beginEdit} className="btn-ghost">
                    Edit
                  </button>
                </>
              )}
            </div>
          )
        }
      >
        {!policy ? (
          <div className="flex flex-col gap-2.5" aria-busy="true">
            <div className="skeleton h-3 w-2/3" />
            <div className="skeleton h-1 w-full" />
            <div className="skeleton h-3 w-1/2" />
            <div className="skeleton h-1 w-full" />
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            <Slider
              id="min-confidence"
              label="LLM min confidence"
              value={
                isEditing
                  ? draft.min_confidence_for_autonomous_action
                  : policy.min_confidence_for_autonomous_action
              }
              display={Number(
                isEditing
                  ? draft.min_confidence_for_autonomous_action
                  : policy.min_confidence_for_autonomous_action,
              ).toFixed(2)}
              min={0.1}
              max={1}
              step={0.05}
              disabled={!isEditing}
              onChange={(v) =>
                setDraft((d) => ({ ...d, min_confidence_for_autonomous_action: v }))
              }
              hint="Below this, the record is escalated to a human instead of acted on."
            />

            <div className="border-t border-surface-3 pt-4">
              <Slider
                id="voice-floor"
                label="Voice call floor"
                value={
                  isEditing ? draft.voice_call_min_amount_inr : policy.voice_call_min_amount_inr
                }
                display={formatINR(
                  isEditing ? draft.voice_call_min_amount_inr : policy.voice_call_min_amount_inr,
                )}
                min={0}
                max={5000}
                step={100}
                disabled={!isEditing}
                onChange={(v) => setDraft((d) => ({ ...d, voice_call_min_amount_inr: v }))}
                hint="Voice is ineligible below this amount. Clearing it does not force a call."
              />
            </div>

            <div className="flex items-start justify-between gap-3 border-t border-surface-3 pt-4">
              <div className="flex flex-col gap-0.5">
                <span className="text-xs text-text-secondary">Adaptive channel bandit</span>
                <p className="text-2xs leading-snug text-text-tertiary">
                  Off falls back to deterministic cost-aware selection.
                </p>
              </div>
              <button
                role="switch"
                aria-checked={
                  isEditing
                    ? draft.enable_adaptive_channel_bandit
                    : policy.enable_adaptive_channel_bandit
                }
                aria-label="Adaptive channel bandit"
                disabled={!isEditing}
                onClick={() =>
                  setDraft((d) => ({
                    ...d,
                    enable_adaptive_channel_bandit: !d.enable_adaptive_channel_bandit,
                  }))
                }
                className={`relative mt-0.5 h-4 w-7 shrink-0 rounded-full transition-colors disabled:opacity-50 ${
                  (
                    isEditing
                      ? draft.enable_adaptive_channel_bandit
                      : policy.enable_adaptive_channel_bandit
                  )
                    ? 'bg-brand-500'
                    : 'bg-surface-4'
                }`}
              >
                <span
                  className={`absolute top-0.5 h-3 w-3 rounded-full bg-white transition-transform ${
                    (
                      isEditing
                        ? draft.enable_adaptive_channel_bandit
                        : policy.enable_adaptive_channel_bandit
                    )
                      ? 'translate-x-3.5'
                      : 'translate-x-0.5'
                  }`}
                />
              </button>
            </div>

            <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 border-t border-surface-3 pt-3 text-2xs">
              <div className="flex justify-between">
                <dt className="text-text-tertiary">Retry cap</dt>
                <dd className="tabular text-text-secondary">{policy.max_retry_attempts}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-text-tertiary">Cooldown</dt>
                <dd className="tabular text-text-secondary">
                  {policy.anti_spam_cooldown_hours}h
                </dd>
              </div>
              <div className="col-span-2 flex justify-between">
                <dt className="text-text-tertiary">Quiet hours</dt>
                <dd className="tabular text-text-secondary">
                  {policy.quiet_hours_start}–{policy.quiet_hours_end}
                </dd>
              </div>
            </dl>
          </div>
        )}
      </Panel>

      {/* ── Adaptive channel bandit ──────────────────────────────────────── */}
      <Panel icon={SlidersHorizontal} title="Channel Bandit">
        {summary?.bandit_stats && Object.keys(summary.bandit_stats).length > 0 ? (
          <Suspense fallback={<div className="skeleton h-[132px] w-full" aria-busy="true" />}>
            <BanditChart stats={summary.bandit_stats} />
          </Suspense>
        ) : (
          <p className="py-3 text-center text-xs text-text-tertiary">
            No learning data yet. Run a batch to populate.
          </p>
        )}
      </Panel>
    </div>
  )
}
