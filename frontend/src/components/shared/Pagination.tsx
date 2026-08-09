import { ChevronLeft, ChevronRight } from 'lucide-react'
import clsx from 'clsx'

interface Props {
  total: number
  offset: number
  limit: number
  onOffsetChange: (offset: number) => void
}

export default function Pagination({ total, offset, limit, onOffsetChange }: Props) {
  if (total <= limit) return null

  const currentPage = Math.floor(offset / limit) + 1
  const totalPages = Math.ceil(total / limit)

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-txt-tertiary">
      <button
        type="button"
        disabled={offset === 0}
        onClick={() => onOffsetChange(Math.max(0, offset - limit))}
        aria-label="Previous page"
        className={clsx(
          'inline-flex h-11 w-11 items-center justify-center rounded-md transition-colors hover:bg-surface-overlay sm:h-8 sm:w-8',
          offset === 0 && 'opacity-30 cursor-not-allowed'
        )}
      >
        <ChevronLeft className="w-3.5 h-3.5" />
      </button>
      <span className="tabular-nums">
        {currentPage} / {totalPages}
      </span>
      <button
        type="button"
        disabled={offset + limit >= total}
        onClick={() => onOffsetChange(offset + limit)}
        aria-label="Next page"
        className={clsx(
          'inline-flex h-11 w-11 items-center justify-center rounded-md transition-colors hover:bg-surface-overlay sm:h-8 sm:w-8',
          offset + limit >= total && 'opacity-30 cursor-not-allowed'
        )}
      >
        <ChevronRight className="w-3.5 h-3.5" />
      </button>
      <span className="ml-1 text-txt-tertiary">{total} total</span>
    </div>
  )
}
