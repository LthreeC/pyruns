import { Inbox } from 'lucide-react'

interface Props {
  title?: string
  description?: string
}

export default function EmptyState({ title = 'No items', description }: Props) {
  return (
    <div className="flex max-w-full flex-col items-center justify-center gap-3 px-4 py-20 text-center text-zinc-500">
      <Inbox className="h-10 w-10 text-zinc-600" />
      <p className="max-w-full break-words text-sm font-medium">{title}</p>
      {description && <p className="max-w-full break-words text-xs text-zinc-600">{description}</p>}
    </div>
  )
}
