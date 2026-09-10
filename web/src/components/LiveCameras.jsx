import { useEffect, useMemo, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import FeedTile from './FeedTile'
import { Link } from '../lib/router'

// The running camera, on the Live screen, over the map.
//
// The map says where a camera is and the feed says what it is looking at, and
// until now the second of those lived only on the Sources screen's wall. This
// is the same FeedTile the wall uses -- the same MJPEG the worker already drew
// its boxes on, the same click targets over them -- so a frame is still
// decoded exactly once, in the worker, and never here.
//
// One feed at a time, not all of them. Every open tile costs the server a
// viewer and keeps a preview alive for as long as it is watched, and this
// screen's job is the map and the feed of what has been read. The wall is
// where several cameras are watched side by side, and the panel says so.
//
// It floats over the map, so it is blurred, like the sighting feed opposite it
// and the boundary toggle above it. Nothing else on the screen is.

const SPRING = { type: 'spring', stiffness: 420, damping: 34 }

export default function LiveCameras({ sources, onOpenBox }) {
  const reduced = useReducedMotion()
  const [open, setOpen] = useState(true)
  const [chosenId, setChosenId] = useState(null)

  const running = useMemo(
    () => sources.filter((s) => s.status === 'running'),
    [sources],
  )

  // Follow the cameras rather than hold a stale id: a source that stops, is
  // deleted, or was never chosen must not leave this panel pointed at nothing.
  useEffect(() => {
    if (running.length === 0) {
      if (chosenId !== null) setChosenId(null)
      return
    }
    if (!running.some((s) => s.source_id === chosenId)) {
      setChosenId(running[0].source_id)
    }
  }, [running, chosenId])

  const shown = running.find((s) => s.source_id === chosenId) || running[0] || null

  // No source at all is the Live screen's own empty state, and it already says
  // what to do. A second panel repeating it would be noise over an empty map.
  if (sources.length === 0) return null

  return (
    <section
      className="absolute bottom-8 right-4 z-[600] w-[24rem] max-w-[calc(100vw-2rem)] overflow-hidden rounded-card glass"
      aria-label="Live camera"
    >
      <header className="flex items-center justify-between gap-3 px-4 py-3">
        <div className="min-w-0">
          <span className="label block">Camera</span>
          <span className="mt-0.5 block truncate text-[13px] text-ink-mid">
            {shown
              ? shown.name
              : `${sources.length} source${sources.length === 1 ? '' : 's'}, none running`}
          </span>
        </div>
        {shown && (
          <button
            type="button"
            onClick={() => setOpen(!open)}
            aria-expanded={open}
            className="shrink-0 rounded-control px-2.5 py-1.5 text-[12.5px] text-ink-mid transition-colors hover:bg-white/5 hover:text-ink-hi focus-visible:outline focus-visible:outline-2 focus-visible:outline-plate-yellow"
          >
            {open ? 'Hide' : 'Show'}
          </button>
        )}
      </header>

      {!shown ? (
        <div className="border-t border-white/5 px-4 py-3">
          <p className="text-[13px] text-ink-mid">
            No camera is running, so there are no frames to show.{' '}
            <Link to="/sources" className="text-ink-hi underline underline-offset-2">
              Start one in Sources
            </Link>{' '}
            and its feed appears here.
          </p>
        </div>
      ) : (
        <AnimatePresence initial={false}>
          {open && (
            <motion.div
              initial={reduced ? false : { height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={reduced ? { opacity: 0 } : { height: 0, opacity: 0 }}
              transition={reduced ? { duration: 0 } : SPRING}
            >
              <div className="border-t border-white/5">
                {/* Which camera, when more than one is running. One row of
                    names, and only one feed is ever mounted -- the others
                    cost the server nothing while they are not watched. */}
                {running.length > 1 && (
                  <div className="flex flex-wrap gap-1.5 px-4 pt-3">
                    {running.map((source) => {
                      const active = source.source_id === shown.source_id
                      return (
                        <button
                          key={source.source_id}
                          type="button"
                          onClick={() => setChosenId(source.source_id)}
                          aria-pressed={active}
                          className={`max-w-[10rem] truncate rounded-control px-2.5 py-1 text-[12.5px] transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-plate-yellow ${
                            active
                              ? 'bg-plate-yellow text-[#151a21] font-semibold'
                              : 'bg-white/5 text-ink-mid hover:text-ink-hi'
                          }`}
                        >
                          {source.name}
                        </button>
                      )
                    })}
                  </div>
                )}

                <div className="p-3">
                  <FeedTile
                    key={shown.source_id}
                    source={shown}
                    onOpenBox={onOpenBox}
                    compact
                  />
                  <p className="px-1 pt-2 text-[12px] text-ink-low">
                    Boxes and plate reads are drawn by the worker that decoded
                    the frame. Click one to open its evidence.{' '}
                    <Link
                      to="/sources"
                      className="text-ink-mid underline underline-offset-2 hover:text-ink-hi"
                    >
                      Camera wall
                    </Link>{' '}
                    shows every running camera at once.
                  </p>
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      )}
    </section>
  )
}
