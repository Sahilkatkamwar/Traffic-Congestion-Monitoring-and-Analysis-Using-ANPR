import { useEffect, useState } from 'react'
import { PRESETS, fromStamp, shortStamp, toStamp } from '../lib/insights'

// The one time filter. Every panel on the screen reads the window this control
// produces, and the server answers all of them from a single row set, so no two
// panels can disagree about what is selected.
//
// The presets measure back from the newest sighting in the database, not from
// the wall clock -- see lib/insights.js for why, and the labels say "of data"
// so the difference is stated rather than inferred.
//
// A datetime-local input speaks local time and every stored timestamp is UTC.
// The conversion happens here, in one place, and the readout underneath shows
// the window that is actually in force.

function toLocalInput(iso) {
  const ms = fromStamp(iso)
  if (ms === null) return ''
  const at = new Date(ms - new Date(ms).getTimezoneOffset() * 60000)
  return at.toISOString().slice(0, 16)
}

function fromLocalInput(value) {
  if (!value) return null
  const ms = new Date(value).getTime()
  return Number.isNaN(ms) ? null : toStamp(ms)
}

export default function TimeWindow({ preset, window: active, extent, onPreset, onCustom }) {
  const [open, setOpen] = useState(preset === 'custom')
  const [from, setFrom] = useState(toLocalInput(active.from))
  const [to, setTo] = useState(toLocalInput(active.to))

  // Follow the preset buttons: picking "24 hours" must leave the custom fields
  // showing the window that is actually in force, not the last thing typed.
  useEffect(() => {
    setFrom(toLocalInput(active.from))
    setTo(toLocalInput(active.to))
  }, [active.from, active.to])

  const covers =
    extent?.first
      ? `Data runs ${shortStamp(extent.first)} to ${shortStamp(extent.last)}`
      : 'No sightings recorded yet'

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <div className="flex items-center gap-1" role="group" aria-label="Time window">
        {PRESETS.map((option) => (
          <button
            key={option.id}
            type="button"
            aria-pressed={preset === option.id}
            title={option.hint || `The last ${option.label} of recorded data`}
            onClick={() => {
              setOpen(false)
              onPreset(option.id)
            }}
            className={`rounded-control px-2.5 py-1.5 text-[12.5px] transition-colors duration-150 ${
              preset === option.id
                ? 'bg-surface-3 font-semibold text-ink-hi'
                : 'text-ink-mid hover:bg-surface-2 hover:text-ink-hi'
            }`}
          >
            {option.label}
          </button>
        ))}
        <button
          type="button"
          aria-pressed={preset === 'custom'}
          aria-expanded={open}
          onClick={() => setOpen((was) => !was)}
          className={`rounded-control px-2.5 py-1.5 text-[12.5px] transition-colors duration-150 ${
            preset === 'custom'
              ? 'bg-surface-3 font-semibold text-ink-hi'
              : 'text-ink-mid hover:bg-surface-2 hover:text-ink-hi'
          }`}
        >
          Custom
        </button>
      </div>

      {open && (
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col gap-1">
            <span className="label">From</span>
            <input
              type="datetime-local"
              value={from}
              onChange={(event) => setFrom(event.target.value)}
              className="rounded-control bg-surface-2 px-2 py-1.5 text-[12.5px] text-ink-hi"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="label">To</span>
            <input
              type="datetime-local"
              value={to}
              onChange={(event) => setTo(event.target.value)}
              className="rounded-control bg-surface-2 px-2 py-1.5 text-[12.5px] text-ink-hi"
            />
          </label>
          <button
            type="button"
            onClick={() => onCustom(fromLocalInput(from), fromLocalInput(to))}
            className="rounded-control bg-plate-yellow px-3 py-1.5 text-[12.5px] font-semibold text-[#1a1400]"
          >
            Apply
          </button>
        </div>
      )}

      <span className="ml-auto text-[11.5px] text-ink-low" title={covers}>
        {covers}
      </span>
    </div>
  )
}
