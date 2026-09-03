import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { useEffect } from 'react'
import PlateString from './PlateString'
import VehicleBadge from './VehicleBadge'
import { cropUrl } from '../lib/api'
import { asPercent, calendarDay, clockTime, parseCandidates } from '../lib/format'

// What one sighting actually is: the crops, the read, how the read was reached,
// and where it happened. Opened from anywhere a sighting is shown.

function Field({ label, children }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className="mt-0.5 text-[14px] tabular-nums text-ink-hi">{children}</div>
    </div>
  )
}

export default function EvidencePanel({ sighting, sourceName, onClose, onTrace }) {
  const reduced = useReducedMotion()

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const candidates = sighting ? parseCandidates(sighting.plate_candidates) : []

  return (
    <AnimatePresence>
      {sighting && (
        <motion.div
          className="absolute inset-0 z-[1200] flex items-center justify-center bg-black/55 p-6"
          initial={reduced ? false : { opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={reduced ? { opacity: 1 } : { opacity: 0 }}
          transition={{ duration: 0.15 }}
          onClick={onClose}
          role="dialog"
          aria-modal="true"
          aria-label="Sighting evidence"
        >
          <motion.div
            className="w-full max-w-2xl overflow-hidden rounded-card bg-surface-1 shadow-float"
            initial={reduced ? false : { opacity: 0, y: 14, scale: 0.985 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduced ? { opacity: 1 } : { opacity: 0, y: 10, scale: 0.99 }}
            transition={reduced ? { duration: 0 } : { type: 'spring', stiffness: 420, damping: 34 }}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-4 p-5">
              <div>
                <PlateString text={sighting.plate_text} conf={sighting.plate_conf} size="lg" />
                <div className="mt-2 flex flex-wrap items-center gap-2 text-[13px] text-ink-mid">
                  <VehicleBadge type={sighting.vehicle_type} />
                  <span>{sourceName}</span>
                  <span aria-hidden="true">·</span>
                  <span className="tabular-nums">
                    {calendarDay(sighting.first_seen_ts)} {clockTime(sighting.first_seen_ts)}
                  </span>
                </div>
              </div>
              <button
                type="button"
                onClick={onClose}
                className="rounded-control px-2 py-1 text-[13px] text-ink-mid transition-colors duration-150 hover:bg-surface-2 hover:text-ink-hi"
              >
                Close
              </button>
            </div>

            <div className="grid gap-3 px-5 sm:grid-cols-[1.4fr_1fr]">
              <figure className="overflow-hidden rounded-card bg-surface-2">
                {cropUrl(sighting.crop_path) ? (
                  <img
                    src={cropUrl(sighting.crop_path)}
                    alt="Vehicle crop"
                    className="max-h-64 w-full object-contain"
                  />
                ) : (
                  <div className="grid h-40 place-items-center px-3 text-center text-[13px] text-ink-low">
                    No vehicle crop was saved for this track.
                  </div>
                )}
                <figcaption className="label px-3 py-2">Vehicle</figcaption>
              </figure>

              <figure className="overflow-hidden rounded-card bg-surface-2">
                {cropUrl(sighting.plate_crop_path) ? (
                  <img
                    src={cropUrl(sighting.plate_crop_path)}
                    alt="Plate crop"
                    className="max-h-40 w-full object-contain"
                  />
                ) : (
                  <div className="grid h-24 place-items-center px-3 text-center text-[13px] text-ink-low">
                    No plate was located on this vehicle.
                  </div>
                )}
                <figcaption className="label px-3 py-2">Plate</figcaption>
              </figure>
            </div>

            <div className="mt-4 grid grid-cols-2 gap-4 px-5 sm:grid-cols-4">
              <Field label="Raw read">{sighting.plate_raw || '--'}</Field>
              <Field label="Confidence">{asPercent(sighting.plate_conf) || '--'}</Field>
              <Field label="Frames voted">{sighting.frames_voted ?? '--'}</Field>
              <Field label="Track">#{sighting.track_id}</Field>
            </div>

            {candidates.length > 0 && (
              <div className="mt-4 px-5">
                <div className="label">Other readings considered</div>
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {candidates.slice(0, 6).map((candidate, index) => {
                    const text = typeof candidate === 'string' ? candidate : candidate.text
                    const conf = typeof candidate === 'string' ? null : candidate.conf
                    return (
                      <span
                        key={String(text) + index}
                        className="rounded-control bg-surface-2 px-2 py-1 font-plate text-[13px] tracking-plate text-ink-mid"
                      >
                        {text}
                        {conf != null && (
                          <span className="ml-1.5 text-[11px] text-ink-low">{asPercent(conf)}</span>
                        )}
                      </span>
                    )
                  })}
                </div>
              </div>
            )}

            <div className="hairline-t mt-5 flex items-center justify-between gap-3 px-5 py-4">
              <p className="text-[12px] text-ink-low">
                {sighting.plate_text
                  ? 'Matching is fuzzy, so tracing returns ranked candidates rather than one answer.'
                  : 'This vehicle has no plate read, so there is nothing to trace it by.'}
              </p>
              <button
                type="button"
                disabled={!sighting.plate_text}
                onClick={() => onTrace?.(sighting.plate_text)}
                className="shrink-0 rounded-control bg-plate-yellow px-3.5 py-2 text-[13px] font-semibold text-[#1a1400] transition-transform duration-150 hover:brightness-105 active:scale-[.98] disabled:cursor-not-allowed disabled:bg-surface-3 disabled:text-ink-low"
              >
                Trace this vehicle
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
