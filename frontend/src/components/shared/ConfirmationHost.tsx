import ConfirmDialog from '@/components/shared/ConfirmDialog'
import { useConfirmationStore } from '@/store'

export default function ConfirmationHost() {
  const request = useConfirmationStore(state => state.request)
  const respond = useConfirmationStore(state => state.respond)

  return (
    <ConfirmDialog
      key={request?.id ?? 0}
      open={Boolean(request)}
      title={request?.title || ''}
      description={request?.description}
      confirmLabel={request?.confirmLabel}
      confirmVariant={request?.confirmVariant}
      onConfirm={() => respond(true)}
      onCancel={() => respond(false)}
    />
  )
}
