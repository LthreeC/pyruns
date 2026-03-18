import clsx from 'clsx'

type InlineMetricTone = 'neutral' | 'amber' | 'emerald' | 'rose' | 'accent'

interface Props {
  label: string
  value: number
  tone?: InlineMetricTone
}

const TONE_STYLES: Record<InlineMetricTone, string> = {
  neutral: 'border-border-subtle bg-surface-overlay text-txt-secondary',
  amber: 'border-amber-500/20 bg-amber-500/10 text-amber-300',
  emerald: 'border-emerald-500/20 bg-emerald-500/10 text-emerald-300',
  rose: 'border-rose-500/20 bg-rose-500/10 text-rose-300',
  accent: 'border-accent/20 bg-accent/8 text-accent',
}

export default function InlineMetric({ label, value, tone = 'neutral' }: Props) {
  return (
    <div
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-2xs',
        TONE_STYLES[tone],
      )}
    >
      <span className="uppercase tracking-[0.16em]">{label}</span>
      <span className={clsx('font-medium tabular-nums', tone === 'neutral' && 'text-txt-primary')}>{value}</span>
    </div>
  )
}
