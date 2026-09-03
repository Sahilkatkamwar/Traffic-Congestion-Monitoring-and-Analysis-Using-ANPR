import { motion, useReducedMotion } from 'framer-motion'
import StatusPill from './StatusPill'
import { Button } from './Field'
import { clockTime, calendarDay } from '../lib/format'

// One source in the list. Elevation and spacing, no borders, no table row.
//
// Progress is only shown for a recorded source, because it is only meaningful
// for one: a live camera has no end to be a fraction of, and the schema stores
// null for it rather than a number that would look like a stalled bar.

const KIND_LABEL = {
  file: 'Recorded',
  webcam: 'Webcam',
  network: 'Network',
  image: 'Image',
}

export default function SourceCard({ source, busy, onStart, onStop, onEdit, onDelete }) {
  const reduced = useReducedMotion()
  const running = source.status === 'running'
  const placed = source.lat != null && source.lon != null
  const progress = typeof source.progress === 'number' ? source.progress : null

  return (
    <motion.article
      layout={!reduced}
      initial={reduced ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={reduced ? { opacity: 1 } : { opacity: 0, y: -6 }}
      transition={reduced ? { duration: 0 } : { type: 'spring', stiffness: 420, damping: 34 }}
      className="rounded-card bg-surface-1 p-4 shadow-lift"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="truncate text-[16px] font-semibold">{source.name}</h3>
            <StatusPill status={source.status} pulse={running} />
            <span className="text-[11px] uppercase tracking-label text-ink-low">
              {KIND_LABEL[source.kind] || source.kind}
            </span>
          </div>
          <p className="mt-1 truncate text-[12.5px] text-ink-mid" title={source.uri}>
            {source.uri}
          </p>
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          {running ? (
            <Button onClick={() => onStop(source)} disabled={busy}>
              Stop
            </Button>
          ) : (
            <Button variant="primary" onClick={() => onStart(source)} disabled={busy}>
              Start
            </Button>
          )}
          <Button variant="quiet" onClick={() => onEdit(source)} disabled={busy}>
            Edit
          </Button>
          <Button variant="quiet" onClick={() => onDelete(source)} disabled={busy}>
            Delete
          </Button>
        </div>
      </div>

      {progress !== null && (
        <div className="mt-3">
          <div className="h-1 overflow-hidden rounded-full bg-surface-2">
            <motion.div
              className="h-full rounded-full bg-plate-yellow"
              initial={false}
              animate={{ width: `${Math.round(progress * 100)}%` }}
              transition={reduced ? { duration: 0 } : { type: 'spring', stiffness: 180, damping: 30 }}
            />
          </div>
          <p className="mt-1 text-[11.5px] tabular-nums text-ink-low">
            {Math.round(progress * 100)}% processed
          </p>
        </div>
      )}

      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1.5 text-[12.5px]">
        <Fact label="Frame skip" value={`every ${source.frame_skip}`} />
        <Fact
          label="fps"
          value={source.fps ? source.fps.toFixed(1) : 'not measured'}
        />
        <Fact
          label="Placed"
          value={
            placed ? `${source.lat.toFixed(4)}, ${source.lon.toFixed(4)}` : 'not on the map'
          }
          weak={!placed}
        />
        {source.start_time && (
          <Fact
            label="Recorded at"
            value={`${calendarDay(source.start_time)} ${clockTime(source.start_time)}`}
          />
        )}
      </dl>

      {source.error && (
        <p
          className={`mt-3 rounded-control px-3 py-2 text-[12.5px] ${
            source.status === 'error'
              ? 'bg-plate-red/15 text-ink-hi'
              : 'bg-surface-2 text-ink-mid'
          }`}
        >
          {source.error}
        </p>
      )}
    </motion.article>
  )
}

function Fact({ label, value, weak = false }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="label">{label}</dt>
      <dd className={`tabular-nums ${weak ? 'text-ink-low' : 'text-ink-mid'}`}>{value}</dd>
    </div>
  )
}
