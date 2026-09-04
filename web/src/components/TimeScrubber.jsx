import { useEffect, useRef } from 'react'
import { clockTime } from '../lib/format'
import { timeline } from '../lib/timeline'

// The transport for a trajectory: play, a track with a tick at every stop, and
// the clock time under the head. The arithmetic behind the positions is in
// lib/timeline.js, because the map and the evidence strip are driven by the
// same numbers and none of the three owns them.

// The whole path plays in about this long, whatever it actually took. A
// trajectory can span hours and nobody is watching it in real time.
const PLAY_MS = 9000
const STEPS = 1000

export default function TimeScrubber({
  stops,
  value,
  onChange,
  playing,
  onPlayingChange,
  activeIndex,
}) {
  const { start, span, positions } = timeline(stops)
  const frameRef = useRef(0)

  // Playback. requestAnimationFrame rather than an interval, so the head moves
  // with the display's refresh instead of stepping, and it stops itself at the
  // end rather than looping -- a journey has an end and pretending otherwise
  // would make the last stop hard to look at.
  useEffect(() => {
    if (!playing) return undefined
    let last = performance.now()
    let current = value >= 0.999 ? 0 : value
    if (current !== value) onChange(0)

    const tick = (now) => {
      const advance = (now - last) / PLAY_MS
      last = now
      current = Math.min(1, current + advance)
      onChange(current)
      if (current >= 1) {
        onPlayingChange(false)
        return
      }
      frameRef.current = requestAnimationFrame(tick)
    }
    frameRef.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frameRef.current)
    // value is deliberately not a dependency: it is the output of this loop,
    // and depending on it would restart the animation on every frame.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, onChange, onPlayingChange])

  const at = span > 0 ? new Date(start + value * span) : null
  const scrubbable = stops.length > 1

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        onClick={() => onPlayingChange(!playing)}
        disabled={!scrubbable}
        aria-label={playing ? 'Pause' : 'Play the path'}
        className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-plate-yellow
          text-[#1a1400] transition-transform duration-150 hover:brightness-105
          active:scale-95 disabled:bg-surface-3 disabled:text-ink-low"
      >
        {playing ? (
          <svg width="12" height="13" viewBox="0 0 12 13" aria-hidden="true">
            <rect x="0" y="0" width="4" height="13" fill="currentColor" rx="1" />
            <rect x="8" y="0" width="4" height="13" fill="currentColor" rx="1" />
          </svg>
        ) : (
          <svg width="12" height="13" viewBox="0 0 12 13" aria-hidden="true">
            <path d="M1 0.5 L12 6.5 L1 12.5 Z" fill="currentColor" />
          </svg>
        )}
      </button>

      <div className="min-w-0 flex-1">
        <div className="relative">
          {/* Ticks sit under the track at each stop's own moment in the span. */}
          <div className="pointer-events-none absolute inset-x-0 top-1/2 h-4 -translate-y-1/2">
            {positions.map((position, index) => (
              <span
                key={stops[index].sighting_id}
                className={`absolute top-1/2 h-2.5 w-[2px] -translate-y-1/2 rounded-full ${
                  index === activeIndex ? 'bg-plate-yellow' : 'bg-ink-low'
                }`}
                style={{ left: `calc(${position * 100}% - 1px)` }}
              />
            ))}
          </div>
          <input
            type="range"
            min={0}
            max={STEPS}
            step={1}
            value={Math.round(value * STEPS)}
            disabled={!scrubbable}
            onChange={(event) => {
              onPlayingChange(false)
              onChange(Number(event.target.value) / STEPS)
            }}
            aria-label="Time through the path"
            aria-valuetext={
              at
                ? `${clockTime(at.toISOString())}, stop ${activeIndex + 1} of ${stops.length}`
                : `Stop ${activeIndex + 1} of ${stops.length}`
            }
            className="relative w-full cursor-pointer appearance-none bg-transparent
              [&::-webkit-slider-runnable-track]:h-[3px]
              [&::-webkit-slider-runnable-track]:rounded-full
              [&::-webkit-slider-runnable-track]:bg-surface-3
              [&::-webkit-slider-thumb]:appearance-none
              [&::-webkit-slider-thumb]:mt-[-5.5px]
              [&::-webkit-slider-thumb]:h-[14px] [&::-webkit-slider-thumb]:w-[14px]
              [&::-webkit-slider-thumb]:rounded-full
              [&::-webkit-slider-thumb]:bg-plate-yellow
              [&::-moz-range-track]:h-[3px] [&::-moz-range-track]:rounded-full
              [&::-moz-range-track]:bg-surface-3
              [&::-moz-range-thumb]:h-[14px] [&::-moz-range-thumb]:w-[14px]
              [&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:rounded-full
              [&::-moz-range-thumb]:bg-plate-yellow
              disabled:cursor-not-allowed"
          />
        </div>
      </div>

      <div className="w-[9.5rem] shrink-0 text-right">
        <div className="text-[13px] tabular-nums text-ink-hi">
          {at ? clockTime(at.toISOString()) : clockTime(stops[activeIndex]?.first_seen_ts)}
        </div>
        <div className="text-[11px] text-ink-low">
          {scrubbable
            ? `stop ${activeIndex + 1} of ${stops.length}`
            : 'seen once -- nothing to play'}
        </div>
      </div>
    </div>
  )
}
