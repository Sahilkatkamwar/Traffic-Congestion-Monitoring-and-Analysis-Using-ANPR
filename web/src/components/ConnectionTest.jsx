import { Button } from './Field'

// The connection test, in one place.
//
// P4b put this inside AddSource's live flow because that flow was the only way
// to add a camera. P8's phone slots are a second way in, and a second copy of
// this panel would be a second thing to keep true: the phone slots would drift
// from the general flow the first time either changed. So the panel moved here
// and both render it.
//
// What it refuses is unchanged -- a live camera is only worth saving once it
// has sent a frame, and the frame is shown so that "connected" is something the
// person can see rather than something the app claims.

export default function ConnectionTest({ uri, tested, testing, onTest, compact = false, label }) {
  return (
    <div className={compact ? '' : 'rounded-card bg-surface-2/60 p-3.5'}>
      <div className="flex items-center justify-between gap-3">
        <div className={compact ? 'sr-only' : ''}>
          <p className="text-[13.5px] font-semibold">Test the connection</p>
          <p className="mt-0.5 text-[12.5px] text-ink-mid">
            A live camera is only worth saving once it has sent a frame.
          </p>
        </div>
        <Button
          onClick={onTest}
          disabled={!uri.trim() || testing}
          aria-label={label ? `Test ${label}` : undefined}
        >
          {testing ? 'Testing…' : 'Test'}
        </Button>
      </div>

      {tested && <TestResult tested={tested} className={compact ? 'mt-2.5' : 'mt-3'} />}
    </div>
  )
}

export function TestResult({ tested, className = '' }) {
  if (!tested) return null
  if (!tested.ok) {
    return <p className={`text-[12.5px] text-plate-red ${className}`}>{tested.error}</p>
  }
  return (
    <div className={`flex gap-3 ${className}`}>
      {tested.preview && (
        <img
          src={tested.preview}
          alt="Frame from the camera being tested"
          className="h-[86px] w-[152px] shrink-0 rounded-control bg-surface-3 object-cover"
        />
      )}
      <div className="min-w-0 text-[12.5px]">
        <p className="font-semibold text-plate-green">Connected.</p>
        <p className="mt-1 tabular-nums text-ink-mid">
          {tested.width}×{tested.height}
          {tested.fps ? ` · ${tested.fps.toFixed(1)} fps` : ''}
          {tested.fps_measured ? ' (measured)' : ''}
        </p>
        {tested.recorded && (
          <p className="mt-1 text-plate-yellow">
            This is a recorded file, not a live camera. It will be timestamped
            from its start time — add it as a recorded video instead.
          </p>
        )}
      </div>
    </div>
  )
}
