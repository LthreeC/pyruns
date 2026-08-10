import clsx from 'clsx'

interface Props {
  checked: boolean
  label: string
  onChange: (checked: boolean) => void
  disabled?: boolean
  title?: string
  className?: string
}

export default function ToggleSwitch({
  checked,
  label,
  onChange,
  disabled = false,
  title,
  className,
}: Props) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={title}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={clsx(
        'touch-target inline-flex h-6 w-11 flex-none items-center justify-center rounded-md outline-none transition-opacity focus-visible:ring-2 focus-visible:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={clsx(
          'relative h-6 w-11 rounded-full border transition-colors',
          checked ? 'border-accent bg-accent' : 'border-border-strong bg-surface-overlay',
        )}
      >
        <span
          className={clsx(
            'absolute left-[3px] top-[3px] h-[18px] w-[18px] rounded-full bg-white shadow-sm transition-transform',
            checked ? 'translate-x-5' : 'translate-x-0',
          )}
        />
      </span>
    </button>
  )
}
