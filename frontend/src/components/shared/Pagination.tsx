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
    <div className="flex items-center gap-2 text-xs text-zinc-400">
      <button
        type="button"
        disabled={offset === 0}
        onClick={() => onOffsetChange(Math.max(0, offset - limit))}
        aria-label="Previous page"
        className={clsx(
          'p-1 rounded hover:bg-surface-overlay transition-colors',
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
          'p-1 rounded hover:bg-surface-overlay transition-colors',
          offset + limit >= total && 'opacity-30 cursor-not-allowed'
        )}
      >
        <ChevronRight className="w-3.5 h-3.5" />
      </button>
      <span className="text-zinc-600 ml-1">{total} total</span>
    </div>
  )
}
