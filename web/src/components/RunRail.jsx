import { motion, useReducedMotion } from 'framer-motion'
import { calendarDay, clockTime, sinceNow } from '../lib/format'

// The list of Analyze runs, beside the result (P6).
//
// A run is a row in `analyze_runs`, so this list is what the server has, not
// what this tab remembers: it survives leaving the screen, reloading the page
// and restarting the app. Everything here is real -- a run with no thumbnail
// yet shows that it has none rather than a grey rectangle standing in for one.
//
// Runs of this session carry live progress; runs from an earlier one carry how
// they ended. Both render the same way because both are the same shape.

const ACTIVE = ['queued', 'running', 'processing']

export const isActive = (status) => ACTIVE.includes(status)

// Drawn rather than imported: no icon set is a dependency here, and one glyph
// does not need one. It takes currentColor, so it turns red on hover with the
// button it sits in.
function Trash() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden className="h-3.5 w-3.5" fill="none"
      stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
      <path d="M2.8 4.2h10.4M6.4 4.2V2.9h3.2v1.3M4.3 4.2l.6 8.4h6.2l.6-8.4M6.7 6.6v3.8M9.3 6.6v3.8" />
    </svg>
  )
}

function Thumb({ run }) {
  if (run.thumbnail) {
    return (
      <img
        src={run.thumbnail}
        alt=""
        className="h-11 w-[68px] shrink-0 rounded-[8px] bg-surface-2 object-cover"
        loading="lazy"
      />
    )
  }
  return (
    <div className="grid h-11 w-[68px] shrink-0 place-items-center rounded-[8px] bg-surface-2 text-[10px] uppercase tracking-label text-ink-low">
      {isActive(run.status) ? 'reading' : 'no frame'}
    </div>
  )
}

function statusText(run) {
  if (run.status === 'queued') {
    return run.queue_position ? `Queued · ${run.queue_position} ahead` : 'Queued'
  }
  if (run.status === 'running' || run.status === 'processing') {
    return run.detail || 'Analyzing'
  }
  if (run.status === 'cancelled') return 'Stopped'
  if (run.status === 'error') return 'Failed'
  return null
}

function Tile({ run, selected, onSelect, onDelete, deleting, reduced }) {
  const active = isActive(run.status)
  const pct = Math.round((run.progress || 0) * 100)
  const note = statusText(run)

  return (
    <motion.div
      layout={reduced ? false : undefined}
      initial={reduced ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={reduced ? { opacity: 1 } : { opacity: 0, x: -12 }}
      transition={{ type: 'spring', stiffness: 420, damping: 34 }}
      className={`group relative rounded-card transition-colors duration-150 ${
        selected ? 'bg-surface-2' : 'hover:bg-surface-1'
      }`}
    >
      <button
        type="button"
        onClick={() => onSelect(run.job_id)}
        aria-current={selected ? 'true' : undefined}
        className="flex w-full items-center gap-2.5 rounded-card p-2 pr-9 text-left"
      >
        <Thumb run={run} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] font-semibold text-ink-hi">
            {run.name || `Run ${run.run_id}`}
          </span>
          <span className="mt-0.5 flex items-center gap-1.5 text-[11.5px] text-ink-low">
            <span className="uppercase tracking-label">
              {run.kind === 'image' ? 'Image' : 'Video'}
            </span>
            <span aria-hidden>·</span>
            <span
              className="tabular-nums"
              title={`${calendarDay(run.created_ts)} ${clockTime(run.created_ts)}`}
            >
              {sinceNow(run.created_ts) || clockTime(run.created_ts)}
            </span>
          </span>
          {note && (
            <span
              className={`mt-1 block text-[11.5px] ${
                run.status === 'error' ? 'text-plate-red' : 'text-ink-mid'
              }`}
            >
              {note}
            </span>
          )}
          {active && (
            <span className="mt-1.5 block h-[3px] overflow-hidden rounded-full bg-surface-3">
              <span
                className={`block h-full rounded-full bg-plate-yellow ${
                  run.status === 'queued' ? 'w-1/4 animate-pulse' : 'transition-[width] duration-200'
                }`}
                style={run.status === 'queued' ? undefined : { width: `${pct}%` }}
              />
            </span>
          )}
        </span>
      </button>

      <button
        type="button"
        onClick={() => onDelete(run)}
        disabled={deleting}
        aria-label={`Delete ${run.name || `run ${run.run_id}`}`}
        title="Delete this run"
        className="absolute right-1.5 top-1.5 rounded-control p-1.5 leading-none
          text-ink-low opacity-0 transition-colors duration-150 hover:bg-surface-3
          hover:text-plate-red focus-visible:opacity-100 group-hover:opacity-100
          disabled:cursor-not-allowed"
      >
        {deleting ? (
          <span className="block h-3.5 w-3.5 text-[11px] leading-[14px]">…</span>
        ) : (
          <Trash />
        )}
      </button>
    </motion.div>
  )
}

export default function RunRail({ runs, selectedId, onSelect, onNew, onDelete, deletingId }) {
  const reduced = useReducedMotion()
  const running = runs.filter((run) => isActive(run.status)).length

  return (
    <aside
      className="flex h-full w-[268px] shrink-0 flex-col"
      style={{ borderRight: '1px solid var(--hairline)' }}
      aria-label="Analyze runs"
    >
      <div className="shrink-0 px-3 pt-4">
        <div className="flex items-baseline justify-between gap-2">
          <span className="label">Runs</span>
          {running > 0 && (
            <span className="text-[11.5px] tabular-nums text-ink-low">
              {running} in progress
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={onNew}
          className="mt-2.5 w-full rounded-control bg-plate-yellow px-3.5 py-2 text-[13px]
            font-semibold text-[#1a1400] transition-colors duration-150 hover:bg-[#ffd23d]"
        >
          New analysis
        </button>
      </div>

      <div className="mt-3 min-h-0 flex-1 space-y-1 overflow-y-auto px-1.5 pb-3">
        {runs.length === 0 ? (
          <p className="px-2 py-3 text-[12.5px] text-ink-mid">
            Nothing analysed yet. Drop an image or a video on the right and it is
            kept here until you delete it.
          </p>
        ) : (
          runs.map((run) => (
            <Tile
              key={run.job_id}
              run={run}
              selected={run.job_id === selectedId}
              onSelect={onSelect}
              onDelete={onDelete}
              deleting={deletingId === run.job_id}
              reduced={reduced}
            />
          ))
        )}
      </div>
    </aside>
  )
}
