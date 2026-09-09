import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import PlateString from './PlateString'
import { clockTime } from '../lib/format'

// P9. What this browser is following, and what each follow has seen.
//
// Follow is live and Trace is history. The difference is the whole reason this
// strip exists rather than a link to the Trace screen: these are sightings that
// have not happened yet at the moment the watch is started, and every one that
// arrives extends the trail on the map beside this.
//
// A session that ends says why. "No longer visible" is a result -- the vehicle
// has not been seen for as long as the server was told to wait -- and a watch
// that quietly stopped watching would be worse than one that was never started.

const REASON = {
  timeout: 'No longer visible.',
  stopped: 'Stopped.',
  disconnected: 'The live feed dropped, so this follow ended.',
}

export default function FollowStrip({
  follows,
  sourceNames,
  onStop,
  onDismiss,
  onTrace,
}) {
  const reduced = useReducedMotion()
  if (!follows || follows.length === 0) return null

  return (
    <div className="mt-3 px-4">
      <AnimatePresence initial={false}>
        {follows.map((entry) => {
          const live = entry.status === 'live'
          const seenAt = entry.stops
            .map((stop) => sourceNames.get(stop.source_id) || stop.source_id)
            .filter((name, index, all) => all.indexOf(name) === index)
          const last = entry.stops[entry.stops.length - 1]

          return (
            <motion.div
              key={entry.follow_id}
              layout={!reduced}
              initial={reduced ? false : { opacity: 0, y: -6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduced ? { opacity: 1 } : { opacity: 0, y: -6 }}
              transition={reduced ? { duration: 0 } : { type: 'spring', stiffness: 380, damping: 32 }}
              className={`mb-1.5 rounded-control px-3 py-2.5 ${
                live ? 'bg-plate-green/15' : 'bg-surface-2'
              }`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="label text-ink-mid">
                      {live ? 'Following' : 'Followed'}
                    </span>
                    {live && (
                      <span className="h-1.5 w-1.5 rounded-full bg-plate-green" aria-hidden="true" />
                    )}
                  </div>
                  <div className="mt-1">
                    <PlateString text={entry.plate_text} conf={entry.plate_conf} size="sm" />
                  </div>
                  <p className="mt-1 text-[12px] text-ink-mid">
                    {entry.stops.length === 1
                      ? `Seen once, at ${seenAt[0]}. Waiting for it at the other cameras.`
                      : `Seen ${entry.stops.length} times, at ${seenAt.join(' → ')}.`}
                    {last?.ts ? ` Last ${clockTime(last.ts)}.` : ''}
                  </p>
                  {!live && (
                    <p className="mt-1 text-[12px] text-ink-low">
                      {REASON[entry.reason] || 'Ended.'}
                    </p>
                  )}
                </div>

                <div className="flex shrink-0 flex-col items-end gap-1.5">
                  <button
                    type="button"
                    onClick={() =>
                      live ? onStop?.(entry.follow_id) : onDismiss?.(entry.follow_id)
                    }
                    className="rounded-control px-2 py-1 text-[12.5px] text-ink-mid transition-colors duration-150 hover:bg-surface-3 hover:text-ink-hi"
                  >
                    {live ? 'Stop' : 'Clear'}
                  </button>
                  {entry.plate_text && (
                    <button
                      type="button"
                      onClick={() => onTrace?.(entry.plate_text)}
                      className="rounded-control px-2 py-1 text-[12.5px] text-ink-mid transition-colors duration-150 hover:bg-surface-3 hover:text-ink-hi"
                      title="Everything already written for this plate"
                    >
                      Trace
                    </button>
                  )}
                </div>
              </div>
            </motion.div>
          )
        })}
      </AnimatePresence>
    </div>
  )
}
