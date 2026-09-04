import { useEffect, useRef } from 'react'
import { useReducedMotion } from 'framer-motion'
import PlateString from './PlateString'
import { cropUrl } from '../lib/api'
import { clockTime } from '../lib/format'

// The crops behind a trajectory, in the order they were recorded.
//
// The strip is driven by the same scrubber the map is, and that synchronisation
// is the claim the screen makes: the crop under the head is the picture the
// vehicle's position on the map was drawn from. Scrolling the active card into
// view is what keeps that true on a path longer than the strip is wide -- a
// highlighted card off-screen is not evidence anybody can see.
//
// Every card is clickable and opens the full evidence for that sighting, which
// is the rule everywhere in this app: a sighting is never shown without a way
// to see what it was.

export default function EvidenceStrip({ stops, activeIndex, onSelect, onOpen }) {
  const reduced = useReducedMotion()
  const railRef = useRef(null)
  const cardRefs = useRef([])

  useEffect(() => {
    const card = cardRefs.current[activeIndex]
    if (!card) return
    card.scrollIntoView({
      behavior: reduced ? 'auto' : 'smooth',
      inline: 'center',
      block: 'nearest',
    })
  }, [activeIndex, reduced])

  return (
    <div
      ref={railRef}
      className="flex gap-2 overflow-x-auto pb-1"
      aria-label="Evidence for each stop"
    >
      {stops.map((stop, index) => {
        const active = index === activeIndex
        const crop = cropUrl(stop.crop_path)
        const plateCrop = cropUrl(stop.plate_crop_path)
        return (
          <button
            key={stop.sighting_id}
            ref={(node) => {
              cardRefs.current[index] = node
            }}
            type="button"
            onClick={() => onSelect?.(index)}
            onDoubleClick={() => onOpen?.(stop)}
            aria-current={active ? 'true' : undefined}
            className={`w-[184px] shrink-0 rounded-card p-2 text-left transition-colors duration-150 ${
              active ? 'bg-surface-2' : 'bg-surface-1/70 hover:bg-surface-2'
            }`}
          >
            <div className="relative h-[92px] overflow-hidden rounded-control bg-surface-3">
              {crop ? (
                <img
                  src={crop}
                  alt={`Vehicle at ${stop.source_name}`}
                  loading="lazy"
                  className="h-full w-full object-cover"
                />
              ) : (
                <span className="grid h-full w-full place-items-center text-[11px] text-ink-low">
                  no crop saved
                </span>
              )}
              <span
                className={`absolute left-1.5 top-1.5 grid h-5 w-5 place-items-center rounded-full
                  text-[11px] font-semibold ${
                    active ? 'bg-plate-yellow text-[#1a1400]' : 'bg-surface-0/80 text-ink-mid'
                  }`}
              >
                {index + 1}
              </span>
              {plateCrop && (
                <img
                  src={plateCrop}
                  alt=""
                  loading="lazy"
                  className="absolute bottom-1.5 right-1.5 h-6 rounded-[4px] bg-surface-0/70 object-contain"
                />
              )}
            </div>

            <div className="mt-2">
              <PlateString text={stop.plate_text} conf={stop.plate_conf} size="sm" />
            </div>
            <div className="mt-1 truncate text-[12px] text-ink-mid">{stop.source_name}</div>
            <div className="text-[11.5px] tabular-nums text-ink-low">
              {clockTime(stop.first_seen_ts)}
              <span className="ml-1.5">
                {Math.round((stop.score ?? 0) * 100)}% match
              </span>
            </div>
          </button>
        )
      })}
    </div>
  )
}
