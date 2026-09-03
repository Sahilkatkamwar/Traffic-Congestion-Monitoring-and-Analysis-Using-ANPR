import { useEffect, useRef, useState } from 'react'
import StatusPill from './StatusPill'
import { streamUrl } from '../lib/api'

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

export default function FeedTile({ source, onOpenSource }) {
  const [nonce, setNonce] = useState(() => Date.now())
  const [state, setState] = useState('connecting')
  const running = source.status === 'running'
  const retryRef = useRef(null)

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

  return (
    <figure className="overflow-hidden rounded-card bg-surface-1 shadow-lift">
      <div className="relative aspect-video bg-[#0a0d12]">
        {running ? (
          <img
            key={nonce}
            src={streamUrl(source.source_id, nonce)}
            alt={`Live feed from ${source.name}`}
            className="h-full w-full object-contain"
            onLoad={() => setState('live')}
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
          </span>
        </button>
        <StatusPill status={source.status} pulse={running} />
      </figcaption>
    </figure>
  )
}
