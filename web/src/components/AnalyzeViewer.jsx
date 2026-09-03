import { useEffect, useMemo, useRef, useState } from 'react'
import { useReducedMotion } from 'framer-motion'

// The annotated result view.
//
// The boxes are drawn here, in the DOM, over a plain frame -- they are not burnt
// into the jpeg by OpenCV. Two reasons, and both are about this screen's promise
// rather than about taste: a burnt-in box is fixed at whatever size the frame was
// written at, and a burnt-in box cannot be clicked, while every detection here
// has to open. It also keeps Hershey-font text out of a screen that otherwise
// sets its type properly.
//
// Box coordinates arrive as fractions of the frame, so they survive the frame
// being written at 720px regardless of what the source resolution was.

const PLAY_MIN_MS = 40
const PLAY_MAX_MS = 400
// Frames fetched ahead of the one on screen. Scrubbing through a video one
// <img src> at a time flickers white between loads; a few warm frames in the
// browser cache is the whole fix and costs nothing else.
const PRELOAD = 4

function timecode(seconds) {
  if (seconds === null || seconds === undefined) return '--'
  const total = Math.max(0, seconds)
  const m = Math.floor(total / 60)
  const s = total - m * 60
  return `${String(m).padStart(2, '0')}:${s.toFixed(2).padStart(5, '0')}`
}

export default function AnalyzeViewer({
  frames,
  vehicles,
  index,
  onIndex,
  selected,
  onSelect,
  isVideo,
}) {
  const reduced = useReducedMotion()
  const [playing, setPlaying] = useState(false)
  const frame = frames[Math.min(index, frames.length - 1)] || null
  const preloadRef = useRef([])

  // Seconds between two written frames, which is what "play" should run at.
  // The frames are strided on a long clip, so this is derived rather than
  // assumed from the source fps.
  const stepMs = useMemo(() => {
    if (frames.length < 2) return 120
    const delta = (frames[1].seconds - frames[0].seconds) * 1000
    return Math.min(PLAY_MAX_MS, Math.max(PLAY_MIN_MS, delta || 120))
  }, [frames])

  useEffect(() => {
    if (!playing) return undefined
    const timer = setInterval(() => {
      onIndex((current) => {
        if (current >= frames.length - 1) {
          setPlaying(false)
          return current
        }
        return current + 1
      })
    }, stepMs)
    return () => clearInterval(timer)
  }, [playing, stepMs, frames.length, onIndex])

  // Stop at the end rather than looping: a result timeline is evidence being
  // reviewed, not a background animation.
  useEffect(() => {
    if (index >= frames.length - 1) setPlaying(false)
  }, [index, frames.length])

  useEffect(() => {
    preloadRef.current = []
    for (let i = 1; i <= PRELOAD; i += 1) {
      const next = frames[index + i]
      if (!next) break
      const img = new Image()
      img.src = next.image
      preloadRef.current.push(img)
    }
  }, [index, frames])

  if (!frame) {
    return (
      <div className="grid h-full place-items-center rounded-card bg-surface-1 p-8 text-center">
        <p className="text-[14px] text-ink-mid">
          This file produced no frames the analysis could read.
        </p>
      </div>
    )
  }

  const byTrack = new Map(vehicles.map((v) => [v.track_id, v]))

  return (
    <div className="flex min-h-0 flex-col gap-3">
      <div className="relative min-h-0 flex-1 overflow-hidden rounded-card bg-black/40">
        <img
          src={frame.image}
          alt={
            isVideo
              ? `Frame ${frame.frame} at ${timecode(frame.seconds)}`
              : 'The analysed image'
          }
          className="h-full w-full object-contain"
          draggable={false}
        />
        {/* One overlay box per detection, positioned in percentages so it stays
            registered to the frame at any size the browser gives the image. */}
        <div className="pointer-events-none absolute inset-0">
          <div className="relative mx-auto h-full w-full">
            {frame.boxes.map((box, i) => {
              const [x1, y1, x2, y2] = box.box
              const active = selected === box.track_id
              const vehicle = byTrack.get(box.track_id)
              const read = box.read || vehicle?.plate_text || null
              return (
                <button
                  key={`${box.track_id}-${i}`}
                  type="button"
                  onClick={() => onSelect(active ? null : box.track_id)}
                  aria-pressed={active}
                  aria-label={`${box.vehicle_type} ${box.track_id}${
                    read ? `, plate ${read}` : ', no plate read'
                  }`}
                  className="pointer-events-auto absolute"
                  style={{
                    left: `${x1 * 100}%`,
                    top: `${y1 * 100}%`,
                    width: `${(x2 - x1) * 100}%`,
                    height: `${(y2 - y1) * 100}%`,
                  }}
                >
                  <span
                    className="absolute inset-0 rounded-[4px] transition-colors duration-150"
                    style={{
                      border: active
                        ? '2.5px solid var(--plate-yellow)'
                        : '1.5px solid rgba(245,197,24,0.72)',
                      background: active ? 'rgba(245,197,24,0.12)' : 'transparent',
                    }}
                  />
                  <span
                    className="absolute left-0 top-full mt-[3px] flex max-w-[220px] items-center
                      gap-1.5 whitespace-nowrap rounded-[5px] bg-[#12161c]/90 px-1.5 py-[2px]
                      text-[10.5px] font-semibold text-ink-hi"
                  >
                    {box.vehicle_type} · {Math.round(box.conf * 100)}%
                    {read && (
                      <span className="font-plate tracking-plate rounded-[3px] bg-plate-white px-1 text-[10.5px] text-[#12161c]">
                        {read}
                      </span>
                    )}
                  </span>
                </button>
              )
            })}
          </div>
        </div>

        {frame.boxes.length === 0 && (
          <div className="absolute bottom-3 left-3 rounded-control bg-[#12161c]/85 px-2.5 py-1.5 text-[12px] text-ink-mid">
            Nothing detected in this frame.
          </div>
        )}
      </div>

      {isVideo && frames.length > 1 && (
        <div className="flex shrink-0 items-center gap-3 rounded-card bg-surface-1 px-3 py-2.5">
          <button
            type="button"
            onClick={() => setPlaying((p) => !p)}
            aria-label={playing ? 'Pause' : 'Play'}
            className="rounded-control bg-surface-3 px-3 py-1.5 text-[13px] font-semibold
              transition-colors duration-150 hover:bg-surface-3/70"
          >
            {playing ? 'Pause' : 'Play'}
          </button>
          <input
            type="range"
            min={0}
            max={frames.length - 1}
            value={index}
            onChange={(event) => {
              setPlaying(false)
              onIndex(Number(event.target.value))
            }}
            aria-label="Scrub the result timeline"
            className="h-1.5 min-w-0 flex-1 cursor-pointer appearance-none rounded-full
              bg-surface-3 accent-[var(--plate-yellow)]"
            style={{ transition: reduced ? 'none' : undefined }}
          />
          <span className="shrink-0 tabular-nums text-[12.5px] text-ink-mid">
            {timecode(frame.seconds)}
          </span>
          <span className="shrink-0 tabular-nums text-[11.5px] text-ink-low">
            {index + 1}/{frames.length}
          </span>
        </div>
      )}
    </div>
  )
}
