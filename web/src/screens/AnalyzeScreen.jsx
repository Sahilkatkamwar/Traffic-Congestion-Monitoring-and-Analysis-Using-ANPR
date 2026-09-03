import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import AnalyzeViewer from '../components/AnalyzeViewer'
import PlateString from '../components/PlateString'
import VehicleBadge from '../components/VehicleBadge'
import Empty from '../components/Empty'
import { Button, Field, Input } from '../components/Field'
import {
  cancelAnalysis,
  exportUrl,
  getAnalysis,
  getFiles,
  startAnalysis,
  uploadFile,
} from '../lib/api'
import { asPercent } from '../lib/format'

// Analyze: drop a file in, get the detections back.
//
// This screen answers to nothing else in the app. It needs no camera, it reads
// no source, and what it produces is never written as a sighting -- an analysis
// is what the models say about a file, and a sighting is a vehicle a placed
// camera saw at a real time. That is why the times here are offsets into the
// media and not clock times: the file has no location and no start time, and
// printing one would be inventing it.

const POLL_MS = 700

const VIDEO_TYPES = ['.mp4', '.mov', '.avi', '.mkv', '.m4v', '.webm', '.mpg', '.mpeg']
const IMAGE_TYPES = ['.jpg', '.jpeg', '.png', '.bmp', '.webp']

const isImageUri = (uri) =>
  IMAGE_TYPES.some((ext) => String(uri || '').toLowerCase().endsWith(ext))

function seconds(value) {
  if (value === null || value === undefined) return '--'
  return `${value.toFixed(2)}s`
}

function Stat({ label, value, hint }) {
  return (
    <div title={hint}>
      <div className="label">{label}</div>
      <div className="mt-0.5 text-count font-semibold tabular-nums text-ink-hi">
        {value}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------- setup

function Picker({ onStart, busy, error }) {
  const [dragging, setDragging] = useState(false)
  const [files, setFiles] = useState([])
  const [uri, setUri] = useState('')
  const [frameSkip, setFrameSkip] = useState(3)
  const [uploading, setUploading] = useState(null)
  const [localError, setLocalError] = useState(null)

  useEffect(() => {
    getFiles()
      .then((list) => setFiles(list.filter((f) => f.kind === 'file' || f.kind === 'image')))
      .catch(() => setFiles([]))
  }, [])

  const upload = useCallback(async (fileList) => {
    const file = fileList && fileList[0]
    if (!file) return
    setLocalError(null)
    setUploading({ name: file.name, progress: 0 })
    try {
      const result = await uploadFile(file, (p) =>
        setUploading({ name: file.name, progress: p }),
      )
      setUri(result.uri)
      setFiles((current) => [
        { uri: result.uri, name: result.name, kind: result.kind, bytes: result.bytes },
        ...current.filter((f) => f.uri !== result.uri),
      ])
    } catch (exc) {
      setLocalError(exc.message)
    } finally {
      setUploading(null)
    }
  }, [])

  const chosenIsImage = isImageUri(uri)
  const shown = localError || error

  return (
    <div className="mx-auto w-full max-w-2xl px-6 py-10">
      <div className="label">Analyze</div>
      <h1 className="mt-1 text-[22px] font-semibold">
        Run the pipeline over one file
      </h1>
      <p className="mt-2 text-body text-ink-mid">
        The same detector, tracker and plate reader the cameras use, pointed at an
        image or a video. No camera has to be configured, and nothing produced here
        is written as a sighting.
      </p>

      <div
        onDragOver={(event) => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragging(false)
          upload(event.dataTransfer.files)
        }}
        className={`mt-6 rounded-card p-6 text-center transition-colors duration-150 ${
          dragging ? 'bg-surface-3' : 'bg-surface-2/60'
        }`}
      >
        {uploading ? (
          <div>
            <p className="text-[13.5px] font-semibold">Uploading {uploading.name}</p>
            <div className="mt-2.5 h-1 overflow-hidden rounded-full bg-surface-3">
              <div
                className="h-full rounded-full bg-plate-yellow transition-[width] duration-150"
                style={{ width: `${Math.round(uploading.progress * 100)}%` }}
              />
            </div>
            <p className="mt-1.5 tabular-nums text-[12px] text-ink-mid">
              {Math.round(uploading.progress * 100)}%
            </p>
          </div>
        ) : (
          <>
            <p className="text-[14px]">Drop an image or a video here, or</p>
            <label className="mt-2.5 inline-block cursor-pointer rounded-control bg-surface-3 px-3.5 py-2 text-[13px] font-semibold transition-colors duration-150 hover:bg-surface-3/70">
              Choose a file
              <input
                type="file"
                accept={[...VIDEO_TYPES, ...IMAGE_TYPES].join(',')}
                className="sr-only"
                onChange={(event) => upload(event.target.files)}
              />
            </label>
          </>
        )}
      </div>

      {files.length > 0 && (
        <div className="mt-5">
          <span className="label block">Or pick one already on disk</span>
          <div className="mt-1.5 max-h-52 overflow-y-auto rounded-card bg-surface-1">
            {files.map((file) => (
              <button
                key={file.uri}
                type="button"
                onClick={() => setUri(file.uri)}
                className={`flex w-full items-center justify-between gap-3 px-3 py-2 text-left
                  text-[13px] transition-colors duration-150 ${
                    uri === file.uri ? 'bg-surface-3' : 'hover:bg-surface-2'
                  }`}
              >
                <span className="min-w-0 truncate">{file.name}</span>
                <span className="shrink-0 text-[11.5px] uppercase tracking-label text-ink-low">
                  {file.kind === 'image' ? 'Image' : 'Video'}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {uri && !chosenIsImage && (
        <div className="mt-5 max-w-[16rem]">
          <Field
            label="Frame skip"
            hint="Process every Nth frame. Higher is faster; too high and the tracker loses vehicles between frames."
          >
            <Input
              type="number"
              min={1}
              max={60}
              value={frameSkip}
              onChange={(event) => setFrameSkip(event.target.value)}
            />
          </Field>
        </div>
      )}

      {shown && (
        <p className="mt-5 rounded-card bg-plate-red/10 px-3.5 py-3 text-[13px] text-plate-red">
          {shown}
        </p>
      )}

      <div className="mt-6 flex items-center gap-3">
        <Button
          variant="primary"
          disabled={!uri || busy}
          onClick={() => onStart(uri, chosenIsImage ? 1 : Number(frameSkip) || 3)}
        >
          {busy ? 'Starting…' : 'Analyze'}
        </Button>
        {uri && (
          <span className="min-w-0 truncate text-[12.5px] text-ink-low">{uri}</span>
        )}
      </div>
    </div>
  )
}

// -------------------------------------------------------------------- running

function Running({ job, onCancel }) {
  const pct = Math.round((job.progress || 0) * 100)
  const indeterminate = job.status === 'queued' || job.stage === 'loading'
  return (
    <div className="grid h-full place-items-center px-6">
      <div className="w-full max-w-md">
        <div className="label">
          {job.status === 'queued' ? 'Queued' : 'Analyzing'}
        </div>
        <h1 className="mt-1 min-w-0 truncate text-[20px] font-semibold">{job.name}</h1>
        <p className="mt-2 text-[13.5px] text-ink-mid">
          {job.status === 'queued'
            ? `One analysis runs at a time so the card is not asked to hold a fourth set of models. This one is ${
                job.queue_position ? `number ${job.queue_position}` : 'next'
              } in the queue.`
            : job.detail || 'Reading the file'}
        </p>

        <div className="mt-4 h-1 overflow-hidden rounded-full bg-surface-2">
          <div
            className={`h-full rounded-full bg-plate-yellow ${
              indeterminate ? 'w-1/3 animate-pulse' : 'transition-[width] duration-200'
            }`}
            style={indeterminate ? undefined : { width: `${pct}%` }}
          />
        </div>
        {!indeterminate && (
          <p className="mt-1.5 tabular-nums text-[12px] text-ink-mid">{pct}%</p>
        )}

        <div className="mt-5">
          <Button onClick={onCancel}>Stop</Button>
        </div>
      </div>
    </div>
  )
}

// --------------------------------------------------------------------- result

function VehicleRow({ vehicle, active, onSelect, reduced }) {
  return (
    <motion.button
      type="button"
      layout={reduced ? false : undefined}
      initial={reduced ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: 'spring', stiffness: 420, damping: 34 }}
      onClick={() => onSelect(active ? null : vehicle.track_id)}
      aria-pressed={active}
      className={`flex w-full items-center gap-3 rounded-card p-2.5 text-left
        transition-colors duration-150 ${
          active ? 'bg-surface-2' : 'hover:bg-surface-1'
        }`}
    >
      {vehicle.crop ? (
        <img
          src={vehicle.crop}
          alt=""
          className="h-14 w-20 shrink-0 rounded-[8px] object-cover"
          loading="lazy"
        />
      ) : (
        <div className="grid h-14 w-20 shrink-0 place-items-center rounded-[8px] bg-surface-2 text-[10.5px] text-ink-low">
          no crop
        </div>
      )}
      <div className="min-w-0 flex-1">
        <PlateString text={vehicle.plate_text} conf={vehicle.plate_conf} size="sm" />
        <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11.5px] text-ink-low">
          <VehicleBadge type={vehicle.vehicle_type} />
          <span className="tabular-nums">
            {seconds(vehicle.first_seconds)} – {seconds(vehicle.last_seconds)}
          </span>
          <span className="tabular-nums">#{vehicle.track_id}</span>
        </div>
      </div>
    </motion.button>
  )
}

function VehicleDetail({ vehicle }) {
  const candidates = vehicle.plate_candidates || []
  return (
    <div className="rounded-card bg-surface-1 p-4">
      <div className="flex items-start gap-4">
        {vehicle.plate_crop && (
          <img
            src={vehicle.plate_crop}
            alt="The plate crop this read came from"
            className="max-h-16 rounded-[8px] bg-black/30 object-contain"
          />
        )}
        <div className="min-w-0">
          <PlateString text={vehicle.plate_text} conf={vehicle.plate_conf} size="md" />
          <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1.5 text-[12.5px] text-ink-mid">
            <span>
              <span className="text-ink-low">raw </span>
              <span className="font-plate tracking-plate">
                {vehicle.plate_raw || '--'}
              </span>
            </span>
            <span className="tabular-nums">
              <span className="text-ink-low">voted over </span>
              {vehicle.frames_voted} frame{vehicle.frames_voted === 1 ? '' : 's'}
            </span>
            <span>
              <span className="text-ink-low">format </span>
              {vehicle.plate_text
                ? vehicle.plate_valid
                  ? `valid${vehicle.plate_state ? ` · ${vehicle.plate_state}` : ''}`
                  : 'not a valid registration'
                : '--'}
            </span>
          </div>
        </div>
      </div>

      {candidates.length > 0 && (
        <div className="hairline-t mt-3.5 pt-3.5">
          <div className="label">Alternatives the vote considered</div>
          <div className="mt-2 flex flex-wrap gap-2">
            {candidates.map((candidate, i) => {
              const text = candidate.text ?? candidate[0] ?? String(candidate)
              const score = candidate.score ?? candidate.conf ?? candidate[1]
              return (
                <span
                  key={`${text}-${i}`}
                  className="flex items-baseline gap-1.5 rounded-control bg-surface-2 px-2 py-1"
                >
                  <span className="font-plate tracking-plate text-[13px]">{text}</span>
                  {score !== undefined && (
                    <span className="tabular-nums text-[11px] text-ink-low">
                      {asPercent(score)}
                    </span>
                  )}
                </span>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

function Result({ job, result, onReset, selected, onSelect, index, onIndex }) {
  const reduced = useReducedMotion()
  const isVideo = result.kind !== 'image'
  const counts = result.counts
  const vehicle = result.vehicles.find((v) => v.track_id === selected) || null

  // Selecting a vehicle jumps the timeline to the first frame it appears in, so
  // the picture and the list never disagree about what is being looked at.
  useEffect(() => {
    if (selected === null) return
    const at = result.frames.findIndex((f) =>
      f.boxes.some((b) => b.track_id === selected),
    )
    if (at >= 0) onIndex(at)
  }, [selected, result.frames, onIndex])

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 flex-wrap items-end justify-between gap-4 px-5 py-4">
        <div className="min-w-0">
          <div className="label">{isVideo ? 'Video analysed' : 'Image analysed'}</div>
          <h1 className="mt-0.5 min-w-0 truncate text-[19px] font-semibold">
            {result.name}
          </h1>
          <p className="mt-1 tabular-nums text-[12px] text-ink-low">
            {result.media.width}×{result.media.height}
            {isVideo && result.media.fps ? ` · ${result.media.fps.toFixed(2)} fps` : ''}
            {` · frame skip ${result.params.frame_skip}`}
            {result.params.frame_stride > 1
              ? ` · timeline shows every ${result.params.frame_stride}th processed frame`
              : ''}
            {` · ${result.elapsed_sec}s`}
          </p>
        </div>

        <div className="flex items-end gap-7">
          <Stat
            label="Vehicles"
            value={counts.vehicles}
            hint="One per tracked vehicle, after stitching -- never one per frame."
          />
          <Stat
            label="Plates read"
            value={counts.plates}
            hint="Vehicles whose plate was read. A vehicle with no read is still a vehicle."
          />
          <Stat label="Frames" value={counts.processed_frames} hint="Frames put through the detector." />
        </div>

        <div className="flex items-center gap-2">
          <a
            href={exportUrl(job.job_id, 'json')}
            className="rounded-control bg-surface-2 px-3.5 py-2 text-[13px] font-semibold text-ink-hi transition-colors duration-150 hover:bg-surface-3"
          >
            Export JSON
          </a>
          <a
            href={exportUrl(job.job_id, 'csv')}
            className="rounded-control bg-surface-2 px-3.5 py-2 text-[13px] font-semibold text-ink-hi transition-colors duration-150 hover:bg-surface-3"
          >
            Export CSV
          </a>
          <Button onClick={onReset}>Analyze another</Button>
        </div>
      </div>

      {result.warning && (
        <p className="mx-5 mb-3 shrink-0 rounded-card bg-plate-yellow/10 px-3.5 py-2.5 text-[13px] text-plate-yellow">
          {result.warning}
        </p>
      )}

      <div className="grid min-h-0 flex-1 gap-4 px-5 pb-5 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div className="flex min-h-0 flex-col gap-3">
          <AnalyzeViewer
            frames={result.frames}
            vehicles={result.vehicles}
            index={index}
            onIndex={onIndex}
            selected={selected}
            onSelect={onSelect}
            isVideo={isVideo}
          />
          <AnimatePresence>
            {vehicle && (
              <motion.div
                className="shrink-0"
                initial={reduced ? false : { opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={reduced ? { opacity: 1 } : { opacity: 0, y: 10 }}
                transition={{ type: 'spring', stiffness: 420, damping: 34 }}
              >
                <VehicleDetail vehicle={vehicle} />
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        <div className="flex min-h-0 flex-col rounded-card bg-surface-1/50">
          <div className="shrink-0 px-3 pt-3">
            <div className="label">
              {result.vehicles.length} vehicle
              {result.vehicles.length === 1 ? '' : 's'}
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
            {result.vehicles.length === 0 ? (
              <Empty
                title="No vehicles were detected."
                action={
                  isVideo
                    ? 'The detector found nothing it calls a car, motorcycle, bus or truck. A lower frame skip sees more frames of the same clip.'
                    : 'The detector found nothing it calls a car, motorcycle, bus or truck in this image.'
                }
              />
            ) : (
              result.vehicles.map((v) => (
                <VehicleRow
                  key={v.track_id}
                  vehicle={v}
                  active={v.track_id === selected}
                  onSelect={onSelect}
                  reduced={reduced}
                />
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------- shell

export default function AnalyzeScreen() {
  const [jobId, setJobId] = useState(null)
  const [job, setJob] = useState(null)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState(null)
  const [index, setIndex] = useState(0)
  const timer = useRef(null)

  const start = useCallback(async (uri, frameSkip) => {
    setBusy(true)
    setError(null)
    try {
      const created = await startAnalysis(uri, frameSkip)
      setResult(null)
      setSelected(null)
      setIndex(0)
      setJob(created)
      setJobId(created.job_id)
    } catch (exc) {
      setError(exc.message)
    } finally {
      setBusy(false)
    }
  }, [])

  // Polled rather than pushed. The websocket carries committed sightings, and an
  // analysis produces none -- putting job progress on it would mean every Live
  // screen in the building receives one person's upload progress.
  useEffect(() => {
    if (!jobId) return undefined
    let cancelled = false

    const tick = async () => {
      try {
        const next = await getAnalysis(jobId)
        if (cancelled) return
        setJob(next)
        if (next.result) setResult(next.result)
        if (['done', 'error', 'cancelled'].includes(next.status)) return
      } catch (exc) {
        if (cancelled) return
        setError(exc.message)
        return
      }
      timer.current = setTimeout(tick, POLL_MS)
    }
    tick()

    return () => {
      cancelled = true
      if (timer.current) clearTimeout(timer.current)
    }
  }, [jobId])

  const reset = useCallback(() => {
    setJobId(null)
    setJob(null)
    setResult(null)
    setSelected(null)
    setIndex(0)
    setError(null)
  }, [])

  const stop = useCallback(async () => {
    if (!jobId) return
    try {
      await cancelAnalysis(jobId)
    } catch (exc) {
      setError(exc.message)
    }
  }, [jobId])

  if (!job) {
    return (
      <div className="h-full overflow-y-auto">
        <Picker onStart={start} busy={busy} error={error} />
      </div>
    )
  }

  if (job.status === 'error') {
    return (
      <div className="grid h-full place-items-center px-6">
        <div className="max-w-md">
          <div className="label">Analysis failed</div>
          <h1 className="mt-1 text-[20px] font-semibold">{job.name}</h1>
          <p className="mt-2 text-body text-plate-red">{job.error}</p>
          <div className="mt-5">
            <Button variant="primary" onClick={reset}>
              Try another file
            </Button>
          </div>
        </div>
      </div>
    )
  }

  if (job.status === 'cancelled') {
    return (
      <div className="grid h-full place-items-center px-6">
        <div className="max-w-md">
          <div className="label">Stopped</div>
          <h1 className="mt-1 text-[20px] font-semibold">{job.name}</h1>
          <p className="mt-2 text-body text-ink-mid">
            The analysis was stopped before it finished, so there is no result to
            show. Start it again to read the whole file.
          </p>
          <div className="mt-5">
            <Button variant="primary" onClick={reset}>
              Choose a file
            </Button>
          </div>
        </div>
      </div>
    )
  }

  if (job.status !== 'done' || !result) {
    return <Running job={job} onCancel={stop} />
  }

  return (
    <Result
      job={job}
      result={result}
      onReset={reset}
      selected={selected}
      onSelect={setSelected}
      index={index}
      onIndex={setIndex}
    />
  )
}
