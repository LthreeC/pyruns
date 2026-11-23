import { ChevronLeft, ChevronRight } from 'lucide-react'
import clsx from 'clsx'
import { useTranslation } from '@/i18n'

interface Props {
  total: number
  offset: number
  limit: number
  onOffsetChange: (offset: number) => void
  disabled?: boolean
  compact?: boolean
  showTotal?: boolean
}

export default function Pagination({ total, offset, limit, onOffsetChange, disabled = false, compact = false, showTotal = true }: Props) {
  const { t } = useTranslation()
  if (total <= limit) return null

  const currentPage = Math.floor(offset / limit) + 1
  const totalPages = Math.ceil(total / limit)

  return (
    <div className={clsx(
      'flex shrink-0 flex-nowrap items-center whitespace-nowrap text-txt-tertiary',
      compact ? 'gap-0.5 text-[10px]' : 'gap-1 text-xs',
    )}>
      <button
        type="button"
        disabled={disabled || offset === 0}
        onClick={() => onOffsetChange(Math.max(0, offset - limit))}
        aria-label={t('Previous page')}
        className={clsx(
          'touch-target inline-flex h-11 w-11 items-center justify-center rounded-md transition-colors hover:bg-surface-overlay sm:h-8 sm:w-8',
          compact && 'h-6 w-6 sm:h-6 sm:w-6',
          (disabled || offset === 0) && 'opacity-30 cursor-not-allowed'
        )}
      >
        <ChevronLeft className="w-3.5 h-3.5" />
      </button>
      <span className={clsx('tabular-nums', compact && 'text-[10px]')}>
        {currentPage} / {totalPages}
      </span>
      <button
        type="button"
        disabled={disabled || offset + limit >= total}
        onClick={() => onOffsetChange(offset + limit)}
        aria-label={t('Next page')}
        className={clsx(
          'touch-target inline-flex h-11 w-11 items-center justify-center rounded-md transition-colors hover:bg-surface-overlay sm:h-8 sm:w-8',
          compact && 'h-6 w-6 sm:h-6 sm:w-6',
          (disabled || offset + limit >= total) && 'opacity-30 cursor-not-allowed'
        )}
      >
        <ChevronRight className="w-3.5 h-3.5" />
      </button>
      {showTotal && <span className={clsx(compact ? 'text-[10px] text-txt-tertiary' : 'ml-1 text-txt-tertiary')}>
        {t('{{total}} total', { total })}
      </span>}
    </div>
  )
}
