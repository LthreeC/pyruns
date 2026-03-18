import { Inbox } from 'lucide-react'

interface Props {
  title?: string
  description?: string
}

export default function EmptyState({ title = 'No items', description }: Props) {
  return (
    <div className="flex flex-col items-center justify-center py-20 gap-3 text-zinc-500">
      <Inbox className="w-10 h-10 text-zinc-600" />
      <p className="text-sm font-medium">{title}</p>
      {description && <p className="text-xs text-zinc-600">{description}</p>}
    </div>
  )
}
