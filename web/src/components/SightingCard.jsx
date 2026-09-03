import { motion, useReducedMotion } from 'framer-motion'
import PlateString from './PlateString'
import VehicleBadge from './VehicleBadge'
import { clockTime, sinceNow } from '../lib/format'
import { cropUrl } from '../lib/api'

// One sighting in the live feed. Every sighting is clickable and every one
// shows its crop -- including the ones with no plate, which are still real
// vehicles and still evidence.

export default function SightingCard({ sighting, sourceName, onOpen, isNew }) {
  const reduced = useReducedMotion()
  const crop = cropUrl(sighting.crop_path)
  const plateCrop = cropUrl(sighting.plate_crop_path)

  return (
    <motion.button
      type="button"
      onClick={() => onOpen?.(sighting)}
      layout={!reduced}
      initial={reduced ? false : { opacity: 0, x: -18 }}
      animate={{ opacity: 1, x: 0 }}
      transition={reduced ? { duration: 0 } : { type: 'spring', stiffness: 460, damping: 34 }}
      className="group w-full rounded-card bg-surface-1/80 p-2.5 text-left
        transition-colors duration-150 hover:bg-surface-2 focus-visible:bg-surface-2"
    >
      <div className="flex gap-3">
        <div className="relative h-[52px] w-[70px] shrink-0 overflow-hidden rounded-control bg-surface-3">
          {crop ? (
            <img
              src={crop}
              alt={`Vehicle at ${sourceName}`}
              loading="lazy"
              className="h-full w-full object-cover"
            />
          ) : (
            <span className="grid h-full w-full place-items-center text-[10px] text-ink-low">
              no crop
            </span>
          )}
          {isNew && (
            <span className="absolute left-1 top-1 h-1.5 w-1.5 rounded-full bg-plate-yellow" />
          )}
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-2">
            <PlateString text={sighting.plate_text} conf={sighting.plate_conf} size="sm" />
            <VehicleBadge type={sighting.vehicle_type} />
          </div>

          <div className="mt-1.5 flex items-center gap-2 text-[12px] text-ink-low">
            <span className="truncate text-ink-mid">{sourceName}</span>
            <span aria-hidden>·</span>
            <span className="tabular-nums" title={sighting.first_seen_ts || ''}>
              {clockTime(sighting.first_seen_ts)}
            </span>
            <span aria-hidden>·</span>
            <span className="tabular-nums">{sinceNow(sighting.first_seen_ts)}</span>
          </div>
        </div>

        {plateCrop && (
          <img
            src={plateCrop}
            alt=""
            loading="lazy"
            className="h-[52px] w-[74px] shrink-0 rounded-control bg-surface-3 object-contain"
          />
        )}
      </div>
    </motion.button>
  )
}
