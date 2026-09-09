import { useEffect, useRef, useState } from 'react'
import StatusPill from './StatusPill'
import { getStreamBoxes, streamUrl } from '../lib/api'

// One tile on the camera wall: the MJPEG stream of one running worker, with the
// boxes and plate reads already drawn on by the worker that decoded the frame.
//
// The <img> is the whole player. A browser renders multipart/x-mixed-replace
// natively, so there is no decode loop here and no canvas -- which also means
// the source is decoded exactly once, in the worker, and never again.
//
// A tile only mounts a stream for a running source. A source that is not
// running has no frames, and the tile says that instead of showing the last
// frame it saw as though the camera were still on.
//
// P9 made the boxes clickable. They are still drawn by the worker, in the
// pixels, so the browser is told separately where they are: /boxes carries the
// same detections as fractions of the frame, and a transparent button is laid
// over each one. Nothing is re-decoded and nothing is drawn twice -- the
// overlay has no fill, and what you click is the box you can see.

// The worker publishes about six frames a second. Asking twice a second keeps
// the click targets under a moving vehicle without a request per frame.
const BOXES_EVERY_MS = 500

export default function FeedTile({ source, onOpenSource, onOpenBox }) {
  const [nonce, setNonce] = useState(() => Date.now())
  const [state, setState] = useState('connecting')
  const [boxes, setBoxes] = useState([])
  // Where the frame actually sits inside the tile. object-contain letterboxes
  // it, so a fraction of the frame is not a fraction of the element.
  const [frame, setFrame] = useState(null)
  const running = source.status === 'running'
  const retryRef = useRef(null)
  const imgRef = useRef(null)

  // A new nonce is a new url, which is what makes the <img> reconnect. Without
  // it a stream that ended stays ended, because the browser will not re-request
  // a url it has already finished loading.
  useEffect(() => {
    if (!running) {
      setState('stopped')
      return undefined
    }
    setState('connecting')
    setNonce(Date.now())
    return () => clearTimeout(retryRef.current)
  }, [running, source.source_id])

  // The click targets. Only while this tile is streaming: the preview the
  // server answers from exists only while somebody is watching, and a tile
  // that is not watching has nothing to ask about.
  useEffect(() => {
    if (!running || !onOpenBox) return undefined
    let cancelled = false
    const tick = () =>
      getStreamBoxes(source.source_id)
        .then((shot) => !cancelled && setBoxes(shot.boxes || []))
        .catch(() => !cancelled && setBoxes([]))
    tick()
    const timer = setInterval(tick, BOXES_EVERY_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
      setBoxes([])
    }
  }, [running, source.source_id, onOpenBox])

  // The letterboxed rectangle the frame occupies, recomputed whenever the
  // element or the frame changes shape.
  const measure = () => {
    const image = imgRef.current
    if (!image || !image.naturalWidth || !image.naturalHeight) return
    const box = image.getBoundingClientRect()
    if (!box.width || !box.height) return
    const scale = Math.min(
      box.width / image.naturalWidth,
      box.height / image.naturalHeight,
    )
    const width = image.naturalWidth * scale
    const height = image.naturalHeight * scale
    setFrame({
      left: (box.width - width) / 2,
      top: (box.height - height) / 2,
      width,
      height,
    })
  }

  useEffect(() => {
    if (!running) {
      setFrame(null)
      return undefined
    }
    const onResize = () => measure()
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [running])

  return (
    <figure className="overflow-hidden rounded-card bg-surface-1 shadow-lift">
      <div className="relative aspect-video bg-[#0a0d12]">
        {running ? (
          <img
            ref={imgRef}
            key={nonce}
            src={streamUrl(source.source_id, nonce)}
            alt={`Live feed from ${source.name}`}
            className="h-full w-full object-contain"
            onLoad={() => {
              setState('live')
              measure()
            }}
            onError={() => {
              setState('lost')
              // The stream ends when the worker stops producing. Reconnecting
              // costs one request, and the endpoint refuses cheaply when the
              // source is not running, so a slow retry is safe.
              clearTimeout(retryRef.current)
              retryRef.current = setTimeout(() => {
                setState('connecting')
                setNonce(Date.now())
              }, 4000)
            }}
          />
        ) : (
          <div className="grid h-full place-items-center px-4 text-center">
            <p className="text-[12.5px] text-ink-low">
              {source.status === 'done'
                ? 'Finished. Start it again to watch it process.'
                : source.status === 'error'
                ? 'This source stopped. Its reason is on the Sources list.'
                : 'Not running. Start it to see its feed.'}
            </p>
          </div>
        )}

        {/* P9. One transparent button per drawn box. No fill and no border of
            its own -- the box is already in the picture, and drawing a second
            one over it would be two boxes disagreeing by a frame. */}
        {running && frame && state === 'live' && onOpenBox &&
          boxes.map((box) => (
            <button
              key={`${box.track_id}`}
              type="button"
              onClick={() => onOpenBox(source, box)}
              title={
                box.plate
                  ? `${box.plate} — ${box.vehicle_type} #${box.track_id}. Open its evidence.`
                  : `${box.vehicle_type} #${box.track_id}. Open its evidence.`
              }
              aria-label={`Open evidence for ${box.vehicle_type} track ${box.track_id}`}
              className="absolute rounded-[4px] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--plate-yellow)]"
              style={{
                left: frame.left + box.x * frame.width,
                top: frame.top + box.y * frame.height,
                width: Math.max(12, box.w * frame.width),
                height: Math.max(12, box.h * frame.height),
              }}
            />
          ))}

        {running && state !== 'live' && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center">
            <p className="text-[12.5px] text-ink-low">
              {state === 'lost' ? 'Feed dropped — reconnecting…' : 'Waiting for the first frame…'}
            </p>
          </div>
        )}
      </div>

      <figcaption className="flex items-center justify-between gap-3 px-3 py-2.5">
        <button
          type="button"
          onClick={() => onOpenSource?.(source)}
          className="min-w-0 rounded-control text-left"
        >
          <span className="block truncate text-[13.5px] font-semibold">{source.name}</span>
          <span className="block truncate text-[11.5px] tabular-nums text-ink-low">
            {source.fps ? `${source.fps.toFixed(1)} fps` : 'fps not measured'}
            {typeof source.progress === 'number'
              ? ` · ${Math.round(source.progress * 100)}%`
              : ''}
            {onOpenBox && boxes.length > 0
              ? ` · ${boxes.length} in frame, click one`
              : ''}
          </span>
        </button>
        <StatusPill status={source.status} pulse={running} />
      </figcaption>
    </figure>
  )
}
