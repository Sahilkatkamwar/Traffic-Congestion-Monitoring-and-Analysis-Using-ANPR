import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

// P10. The Boundaries toggle, and what the map is currently able to tell you
// about where you clicked.
//
// It floats over the map, so it is blurred -- the one surface treatment that
// means "above the map" on this screen and is used nowhere else. It sits under
// the zoom control on the right, opposite the sighting feed, because the feed
// is the screen and this is a setting for the thing behind it.
//
// The administrative chain is rendered outermost first, State to Village, and
// the level is labelled next to every name: "Nashik" alone is a district and a
// city and a taluka, and which one is the entire point of asking.

const SPRING = { type: 'spring', stiffness: 420, damping: 34 }

function Row({ area, innermost }) {
  return (
    <li className="flex items-baseline justify-between gap-3">
      <span
        className={
          innermost
            ? 'text-[14px] font-semibold text-ink-hi'
            : 'text-[13px] text-ink-mid'
        }
      >
        {area.name}
      </span>
      <span className="label shrink-0 text-[10px] text-ink-low">{area.level_label}</span>
    </li>
  )
}

export default function BoundaryPanel({ on, onToggle, status }) {
  const reduced = useReducedMotion()
  const state = status?.state

  return (
    <div className="w-[16.5rem] max-w-[calc(100vw-2rem)] overflow-hidden rounded-card glass">
      <button
        type="button"
        onClick={() => onToggle(!on)}
        aria-pressed={on}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-white/5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-plate-yellow"
      >
        <span>
          <span className="label block">Boundaries</span>
          <span className="mt-0.5 block text-[13px] text-ink-mid">
            {/* What is actually drawn, which depends on the zoom -- districts
                and talukas, or talukas and the towns in them. Never "state":
                a state outline is 12-27 MB out of Overpass and is not drawn,
                though a click still names the state it found. */}
            {on ? (status?.levels?.join(' and ') ?? 'District and taluka') : 'Off'}
          </span>
        </span>
        <span
          className={`h-5 w-9 shrink-0 rounded-full p-0.5 transition-colors ${
            on ? 'bg-plate-yellow' : 'bg-white/15'
          }`}
        >
          <motion.span
            layout
            transition={reduced ? { duration: 0 } : SPRING}
            className={`block h-4 w-4 rounded-full bg-surface-1 ${on ? 'ml-4' : ''}`}
          />
        </span>
      </button>

      <AnimatePresence initial={false}>
        {on && (
          <motion.div
            initial={reduced ? false : { height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={reduced ? { opacity: 0 } : { height: 0, opacity: 0 }}
            transition={reduced ? { duration: 0 } : SPRING}
          >
            <div className="border-t border-white/5 px-4 py-3">
              {state === 'loading' && (
                <p className="text-[13px] text-ink-mid">Loading outlines…</p>
              )}

              {state === 'identifying' && (
                <p className="text-[13px] text-ink-mid">Finding that area…</p>
              )}

              {state === 'error' && (
                <p className="text-[13px] text-plate-red">{status.detail}</p>
              )}

              {state === 'empty' && (
                <p className="text-[13px] text-ink-mid">
                  No mapped boundary covers this view. Zoom in on a town or a
                  district and its outline appears.
                </p>
              )}

              {state === 'ready' && (
                <p className="text-[13px] text-ink-mid">
                  {status.count} outline{status.count === 1 ? '' : 's'} drawn
                  {status.levels?.length ? ` — ${status.levels.join(' and ')}` : ''}.
                  Click the map to name the area you are in.
                  {status.stale ? ' Drawn from the last answer that arrived.' : ''}
                </p>
              )}

              {state === 'at' &&
                (status.areas?.length ? (
                  <ul className="flex flex-col gap-1.5">
                    {status.areas.map((area, index) => (
                      <Row
                        key={`${area.id}-${area.admin_level}`}
                        area={area}
                        innermost={index === status.areas.length - 1}
                      />
                    ))}
                  </ul>
                ) : (
                  <p className="text-[13px] text-ink-mid">
                    That point is not inside any mapped administrative area.
                  </p>
                ))}

              {!state && (
                <p className="text-[13px] text-ink-mid">
                  Outlines from OpenStreetMap. Click the map to name the state,
                  district and taluka a point sits in.
                </p>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
