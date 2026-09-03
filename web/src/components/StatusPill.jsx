// A source's status, as one pill. The only fully-rounded shape in the app --
// that is what marks it as a status rather than a control.
//
// Colour matches the map marker exactly, because a person reading a red dot on
// the map and a red pill in the list has to be reading the same fact.

const STATUS = {
  running: { label: 'Running', dot: 'bg-plate-green', text: 'text-plate-green' },
  idle: { label: 'Idle', dot: 'bg-plate-yellow', text: 'text-plate-yellow' },
  done: { label: 'Done', dot: 'bg-ink-low', text: 'text-ink-mid' },
  error: { label: 'Error', dot: 'bg-plate-red', text: 'text-plate-red' },
}

export default function StatusPill({ status, pulse = false }) {
  const style = STATUS[status] || { label: status || 'Unknown', dot: 'bg-ink-low', text: 'text-ink-mid' }
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full bg-surface-2 px-2.5 py-[3px]
        text-[11px] font-semibold ${style.text}`}
    >
      <span className="relative flex h-1.5 w-1.5">
        {pulse && (
          <span className={`marker-pulse absolute inset-0 rounded-full ${style.dot}`} />
        )}
        <span className={`relative h-1.5 w-1.5 rounded-full ${style.dot}`} />
      </span>
      {style.label}
    </span>
  )
}
