import { motion, useReducedMotion } from 'framer-motion'
import PlateString from './PlateString'
import VehicleBadge from './VehicleBadge'
import { cropUrl } from '../lib/api'
import { calendarDay, clockTime, durationText, sinceNow } from '../lib/format'

// One alert, with the evidence it is about.
//
// The two kinds are one component because they are one thing on the screen -- a
// claim, its severity, and the pictures it rests on. What differs is how many
// pictures: a blacklist hit is one sighting, an impossible transition is a pair
// and is unreadable as anything else. Both crops side by side, both timestamps,
// the distance between the sources and the speed that would have been required
// is not a nice presentation of that alert, it IS the alert -- the number is
// the whole reason to doubt one of the two reads.
//
// Colour carries severity and nothing else here. Red is the government plate
// ground and is spent on `critical`; yellow, the commercial ground and this
// app's accent, on `warning`; `info` takes no colour at all, because an alert
// that does not need attention should not be competing for it.

const SEVERITY = {
  critical: {
    label: 'Critical',
    pill: 'bg-plate-red text-[#fff2f0]',
    rail: 'var(--plate-red)',
  },
  warning: {
    label: 'Warning',
    pill: 'bg-plate-yellow text-[#1a1400]',
    rail: 'var(--plate-yellow)',
  },
  info: {
    label: 'Info',
    pill: 'bg-surface-3 text-ink-mid',
    rail: 'var(--hairline)',
  },
}

const KIND = {
  blacklist: 'Blacklist',
  impossible_transition: 'Impossible transition',
}

function Crop({ sighting, onOpen, caption }) {
  const crop = cropUrl(sighting.crop_path)
  const plateCrop = cropUrl(sighting.plate_crop_path)
  const where = sighting.source_name || sighting.source_id

  return (
    <button
      type="button"
      onClick={() => onOpen?.(sighting)}
      className="group min-w-0 flex-1 rounded-card bg-surface-2/60 p-2 text-left
        transition-colors duration-150 hover:bg-surface-2 focus-visible:bg-surface-2"
      title={`Open the evidence for this sighting at ${where}`}
    >
      {caption && <div className="label mb-1.5 px-0.5">{caption}</div>}

      <div className="aspect-[4/3] w-full overflow-hidden rounded-control bg-surface-3">
        {crop ? (
          <img
            src={crop}
            alt={`Vehicle at ${where}`}
            loading="lazy"
            className="h-full w-full object-cover"
          />
        ) : (
          <span className="grid h-full w-full place-items-center text-[11px] text-ink-low">
            no crop saved
          </span>
        )}
      </div>

      {plateCrop && (
        <img
          src={plateCrop}
          alt=""
          loading="lazy"
          className="mt-1.5 h-[38px] w-full rounded-control bg-surface-3 object-contain"
        />
      )}

      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1">
        <PlateString text={sighting.plate_text} conf={sighting.plate_conf} size="sm" />
        <VehicleBadge type={sighting.vehicle_type} />
      </div>

      <div className="mt-1.5 text-[12px] text-ink-low">
        <div className="truncate text-ink-mid">{where}</div>
        <div className="tabular-nums" title={sighting.first_seen_ts || ''}>
          {clockTime(sighting.first_seen_ts)}
          {calendarDay(sighting.first_seen_ts) && (
            <span className="ml-1.5 text-ink-low">
              {calendarDay(sighting.first_seen_ts)}
            </span>
          )}
        </div>
      </div>
    </button>
  )
}

// The arithmetic, spelled out. An alert that says "impossible" without showing
// the sum is asking to be believed; this one can be checked.
function Leg({ leg }) {
  const gap = durationText(leg.gap_seconds)
  return (
    <div className="flex flex-wrap items-center justify-center gap-x-5 gap-y-2 px-2 py-1 text-center">
      <div>
        <div className="label">Apart</div>
        <div className="text-[17px] font-semibold tabular-nums">
          {leg.distance_km === null ? '--' : `${leg.distance_km.toFixed(2)} km`}
        </div>
      </div>
      <div>
        <div className="label">Between</div>
        <div className="text-[17px] font-semibold tabular-nums">{gap || '--'}</div>
      </div>
      <div>
        <div className="label">Would need</div>
        <div className="text-[17px] font-semibold tabular-nums text-plate-red">
          {leg.speed_kmh === null
            ? 'no time at all'
            : `${Math.round(leg.speed_kmh).toLocaleString()} km/h`}
        </div>
      </div>
      <div>
        <div className="label">Plausible up to</div>
        <div className="text-[17px] font-semibold tabular-nums text-ink-mid">
          {Math.round(leg.limit_kmh)} km/h
        </div>
      </div>
    </div>
  )
}

export default function AlertCard({ alert, isNew = false, onOpenSighting }) {
  const reduced = useReducedMotion()
  const severity = SEVERITY[alert.severity] || SEVERITY.info
  const stops = alert.sightings || []
  const paired = alert.kind === 'impossible_transition' && stops.length >= 2

  return (
    <motion.article
      layout={!reduced}
      initial={reduced ? false : { opacity: 0, y: -12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={reduced ? { duration: 0 } : { type: 'spring', stiffness: 420, damping: 34 }}
      className="overflow-hidden rounded-card bg-surface-1"
      style={{ boxShadow: 'var(--shadow-lift)' }}
      aria-label={`${severity.label} alert: ${alert.detail}`}
    >
      {/* The one hairline this card gets, carrying severity. Cheaper than a
          border on everything and it says something. */}
      <div className="h-[3px] w-full" style={{ background: severity.rail }} />

      <div className="p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span
                className={`rounded-full px-2 py-[2px] text-[11px] font-semibold ${severity.pill}`}
              >
                {severity.label}
              </span>
              <span className="label">{KIND[alert.kind] || alert.kind}</span>
              {isNew && (
                <span
                  className="h-1.5 w-1.5 rounded-full bg-plate-yellow"
                  title="Raised since this screen opened"
                />
              )}
            </div>
            <p className="mt-2 max-w-[68ch] text-[14.5px] text-ink-hi">{alert.detail}</p>
          </div>

          <div className="shrink-0 text-right text-[12px] text-ink-low">
            <div className="tabular-nums" title={alert.created_ts || ''}>
              {clockTime(alert.created_ts)}
            </div>
            <div className="tabular-nums">{sinceNow(alert.created_ts)}</div>
          </div>
        </div>

        {stops.length > 0 && (
          <div className="mt-3">
            {paired ? (
              <div className="flex flex-col gap-2">
                <div className="flex items-stretch gap-2">
                  <Crop
                    sighting={stops[0]}
                    onOpen={onOpenSighting}
                    caption={`Seen first · ${stops[0].source_name || stops[0].source_id}`}
                  />
                  <Crop
                    sighting={stops[stops.length - 1]}
                    onOpen={onOpenSighting}
                    caption={`Then · ${
                      stops[stops.length - 1].source_name ||
                      stops[stops.length - 1].source_id
                    }`}
                  />
                </div>
                {alert.transition && <Leg leg={alert.transition} />}
              </div>
            ) : (
              <div className="flex items-stretch gap-2">
                {stops.map((sighting) => (
                  <div key={sighting.sighting_id} className="max-w-[15rem] flex-1">
                    <Crop sighting={sighting} onOpen={onOpenSighting} />
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* A sighting deleted with its source is named rather than quietly
            dropped. An alert pointing at nothing should say so. */}
        {alert.missing_sightings?.length > 0 && (
          <p className="mt-2 text-[12px] text-ink-low">
            {alert.missing_sightings.length} sighting
            {alert.missing_sightings.length === 1 ? '' : 's'} behind this alert
            {alert.missing_sightings.length === 1 ? ' is' : ' are'} no longer in the
            database — deleted with the source. The alert is kept as a record.
          </p>
        )}
      </div>
    </motion.article>
  )
}
