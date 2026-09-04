import { useCallback, useEffect, useMemo, useState } from 'react'
import Modal from './Modal'
import MapPicker from './MapPicker'
import { Button, Field, Input, Radio, Select } from './Field'
import {
  createSource,
  getDevices,
  getFiles,
  testSource,
  uploadFile,
} from '../lib/api'

// The three ways a source gets added. One dialog, three flows, because they
// differ in what they need before they can be saved:
//
//   live      needs proof it connects, which is a frame, before it is worth
//             saving at all
//   recorded  needs a start_time, or every timestamp it produces is a guess
//   image     needs neither, and is one shot
//
// Everything they have in common -- name, placement, frame_skip -- is the same
// panel underneath.

const FLOWS = [
  {
    id: 'live',
    title: 'Live camera',
    detail: 'A webcam on this machine, a phone on the network, or an RTSP stream.',
  },
  {
    id: 'recorded',
    title: 'Recorded video',
    detail: 'Upload a clip or pick one already on disk. Needs the time it was filmed.',
  },
  {
    id: 'image',
    title: 'Image',
    detail: 'One or more stills, read once each.',
  },
]

const bytes = (n) => {
  if (!Number.isFinite(n)) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(0)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(2)} GB`
}

// datetime-local wants 'YYYY-MM-DDTHH:MM' in local time, with no zone.
function localNow() {
  const now = new Date()
  now.setMinutes(now.getMinutes() - now.getTimezoneOffset())
  return now.toISOString().slice(0, 16)
}

export default function AddSource({ open, onClose, onAdded, sources }) {
  const [flow, setFlow] = useState('live')
  const [name, setName] = useState('')
  const [uri, setUri] = useState('')
  const [place, setPlace] = useState({ lat: null, lon: null })
  const [frameSkip, setFrameSkip] = useState(3)
  const [startTime, setStartTime] = useState(localNow())

  const [devices, setDevices] = useState(null)
  const [devicesError, setDevicesError] = useState(null)
  const [files, setFiles] = useState([])
  const [tested, setTested] = useState(null)
  const [testing, setTesting] = useState(false)
  const [uploading, setUploading] = useState(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const reset = useCallback(() => {
    setFlow('live')
    setName('')
    setUri('')
    setPlace({ lat: null, lon: null })
    setFrameSkip(3)
    setStartTime(localNow())
    setTested(null)
    setTesting(false)
    setUploading(null)
    setSaving(false)
    setError(null)
  }, [])

  useEffect(() => {
    if (!open) return
    reset()
    getFiles().then(setFiles).catch(() => setFiles([]))
  }, [open, reset])

  // Webcam detection opens every index in turn, which takes seconds, so it only
  // runs when the live flow is actually on screen.
  useEffect(() => {
    if (!open || flow !== 'live' || devices !== null) return
    let cancelled = false
    getDevices()
      .then((found) => !cancelled && setDevices(found))
      .catch((err) => {
        if (cancelled) return
        setDevices({ webcams: [], busy: [] })
        setDevicesError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [open, flow, devices])

  // A new uri is a different camera, so whatever the last one proved no longer
  // applies. Silently keeping a stale green tick is how a broken source gets
  // saved as tested.
  useEffect(() => {
    setTested(null)
  }, [uri])

  const kind = useMemo(() => {
    if (flow === 'image') return 'image'
    if (flow === 'recorded') return 'file'
    if (/^\d+$/.test(uri.trim())) return 'webcam'
    return 'network'
  }, [flow, uri])

  const selectable = files.filter((f) =>
    flow === 'image' ? f.kind === 'image' : f.kind === 'file',
  )

  async function runTest() {
    setTesting(true)
    setTested(null)
    setError(null)
    try {
      setTested(await testSource(uri.trim()))
    } catch (err) {
      setTested({ ok: false, error: err.message })
    } finally {
      setTesting(false)
    }
  }

  async function handleUpload(fileList) {
    const chosen = [...fileList]
    if (chosen.length === 0) return
    setError(null)
    // Several stills at once is the specified image flow; a video is one file.
    if (flow === 'image' && chosen.length > 1) {
      const added = []
      try {
        for (const [index, file] of chosen.entries()) {
          setUploading({ name: file.name, progress: 0, index: index + 1, total: chosen.length })
          const result = await uploadFile(file, (p) =>
            setUploading((u) => (u ? { ...u, progress: p } : u)),
          )
          const created = await createSource({
            name: file.name.replace(/\.[^.]+$/, ''),
            uri: result.uri,
            kind: 'image',
            lat: place.lat,
            lon: place.lon,
            frame_skip: 1,
          })
          added.push(created.source)
        }
        onAdded?.(added, `Added ${added.length} images. They are being read now.`)
        onClose?.()
      } catch (err) {
        setError(err.message)
      } finally {
        setUploading(null)
      }
      return
    }

    const file = chosen[0]
    try {
      setUploading({ name: file.name, progress: 0, index: 1, total: 1 })
      const result = await uploadFile(file, (p) =>
        setUploading((u) => (u ? { ...u, progress: p } : u)),
      )
      setUri(result.uri)
      if (!name.trim()) setName(file.name.replace(/\.[^.]+$/, ''))
    } catch (err) {
      setError(err.message)
    } finally {
      setUploading(null)
    }
  }

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const payload = {
        name: name.trim(),
        uri: uri.trim(),
        kind,
        lat: place.lat,
        lon: place.lon,
        frame_skip: flow === 'image' ? 1 : Number(frameSkip),
      }
      if (flow === 'recorded' && startTime) payload.start_time = startTime
      const result = await createSource(payload)
      onAdded?.([result.source], result.message)
      onClose?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  const needsTest = flow === 'live'
  const canSave =
    name.trim() &&
    uri.trim() &&
    !saving &&
    !uploading &&
    (!needsTest || tested?.ok)

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title="Add a source"
      sub="A source starts processing as soon as it is added."
      footer={
        <>
          <Button variant="quiet" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={save} disabled={!canSave}>
            {saving ? 'Adding…' : 'Add source'}
          </Button>
        </>
      }
    >
      <div className="grid gap-5 md:grid-cols-[15rem_minmax(0,1fr)]">
        <div className="flex flex-col gap-2">
          {FLOWS.map((option) => (
            <Radio
              key={option.id}
              name="add-flow"
              value={option.id}
              checked={flow === option.id}
              onChange={(next) => {
                setFlow(next)
                setUri('')
                setTested(null)
              }}
              title={option.title}
              detail={option.detail}
            />
          ))}
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          {flow === 'live' && (
            <LiveFlow
              uri={uri}
              setUri={setUri}
              setName={setName}
              name={name}
              devices={devices}
              devicesError={devicesError}
              onRescan={() => {
                setDevices(null)
                setDevicesError(null)
              }}
              tested={tested}
              testing={testing}
              onTest={runTest}
            />
          )}

          {flow !== 'live' && (
            <FileFlow
              flow={flow}
              uri={uri}
              setUri={setUri}
              name={name}
              setName={setName}
              files={selectable}
              uploading={uploading}
              onUpload={handleUpload}
            />
          )}

          <Field label="Name" hint="Shown on the map, in the feed, and on every sighting.">
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="North gate"
            />
          </Field>

          <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            {flow === 'recorded' && (
              <Field
                label="Recorded at"
                hint="The wall-clock time filming began. Every timestamp is measured from it."
              >
                <Input
                  type="datetime-local"
                  value={startTime}
                  onChange={(e) => setStartTime(e.target.value)}
                />
              </Field>
            )}
            {flow !== 'image' && (
              <Field
                label="Frame skip"
                hint="Process every Nth frame. Higher is cheaper; too high loses tracks."
              >
                <Select value={frameSkip} onChange={(e) => setFrameSkip(e.target.value)}>
                  <option value={1}>1 — every frame</option>
                  <option value={2}>2</option>
                  <option value={3}>3 — default</option>
                  <option value={5}>5</option>
                  <option value={8}>8</option>
                </Select>
              </Field>
            )}
          </div>

          <div>
            <span className="label block">Location</span>
            <div className="mt-1.5">
              <MapPicker value={place} onPick={setPlace} others={sources} height={240} />
            </div>
          </div>

          {error && (
            <p
              role="alert"
              className="rounded-control bg-plate-red/15 px-3 py-2 text-[13px] text-ink-hi"
            >
              {error}
            </p>
          )}
        </div>
      </div>
    </Modal>
  )
}

function LiveFlow({ uri, setUri, name, setName, devices, devicesError, onRescan, tested, testing, onTest }) {
  return (
    <div className="flex flex-col gap-4">
      <div>
        <div className="flex items-baseline justify-between">
          <span className="label">Detected cameras</span>
          <button
            type="button"
            onClick={onRescan}
            className="rounded-control px-2 py-1 text-[12px] text-ink-low transition-colors duration-150 hover:bg-surface-2 hover:text-ink-hi"
          >
            Scan again
          </button>
        </div>

        <div className="mt-1.5">
          {devices === null ? (
            <p className="text-[13px] text-ink-low">
              Looking for cameras — each one has to be opened to be found, so this
              takes a few seconds.
            </p>
          ) : devices.webcams.length === 0 ? (
            <p className="text-[13px] text-ink-low">
              {devicesError
                ? devicesError
                : devices.busy?.length
                ? `No free camera found. Index ${devices.busy.join(', ')} is already open by a running source.`
                : 'No camera answered. Paste a phone or RTSP URL below instead.'}
            </p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {devices.webcams.map((device) => (
                <button
                  key={device.index}
                  type="button"
                  onClick={() => {
                    setUri(String(device.index))
                    if (!name.trim()) setName(`Webcam ${device.index}`)
                  }}
                  className={`rounded-control px-3 py-2 text-left text-[13px] transition-colors duration-150 ${
                    uri === String(device.index)
                      ? 'bg-surface-3 text-ink-hi'
                      : 'bg-surface-2 text-ink-mid hover:bg-surface-3 hover:text-ink-hi'
                  }`}
                >
                  <span className="block font-semibold text-ink-hi">Camera {device.index}</span>
                  <span className="tabular-nums">
                    {device.width}×{device.height}
                    {device.fps ? ` · ${device.fps.toFixed(0)} fps` : ''}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <Field
        label="Or a URL"
        hint="A phone camera app gives you something like http://192.168.1.7:8080/video. RTSP works too."
      >
        <Input
          value={uri}
          onChange={(e) => setUri(e.target.value)}
          placeholder="http://192.168.1.7:8080/video"
          spellCheck={false}
        />
      </Field>

      <div className="rounded-card bg-surface-2/60 p-3.5">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-[13.5px] font-semibold">Test the connection</p>
            <p className="mt-0.5 text-[12.5px] text-ink-mid">
              A live camera is only worth saving once it has sent a frame.
            </p>
          </div>
          <Button onClick={onTest} disabled={!uri.trim() || testing}>
            {testing ? 'Testing…' : 'Test'}
          </Button>
        </div>

        {tested && (
          <div className="mt-3">
            {tested.ok ? (
              <div className="flex gap-3">
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
            ) : (
              <p className="text-[12.5px] text-plate-red">{tested.error}</p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function FileFlow({ flow, uri, setUri, name, setName, files, uploading, onUpload }) {
  const [dragging, setDragging] = useState(false)
  const accept = flow === 'image' ? 'image/*' : 'video/*'
  const multiple = flow === 'image'

  return (
    <div className="flex flex-col gap-4">
      <div
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          onUpload(e.dataTransfer.files)
        }}
        className={`rounded-card p-5 text-center transition-colors duration-150 ${
          dragging ? 'bg-surface-3' : 'bg-surface-2/60'
        }`}
      >
        {uploading ? (
          <div>
            <p className="text-[13.5px] font-semibold">
              Uploading {uploading.name}
              {uploading.total > 1 ? ` (${uploading.index} of ${uploading.total})` : ''}
            </p>
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
            <p className="text-[13.5px]">
              Drop {flow === 'image' ? 'images' : 'a video'} here, or
            </p>
            <label className="mt-2 inline-block cursor-pointer rounded-control bg-surface-3 px-3.5 py-2 text-[13px] font-semibold transition-colors duration-150 hover:bg-surface-3/70">
              Choose {multiple ? 'files' : 'a file'}
              <input
                type="file"
                accept={accept}
                multiple={multiple}
                className="sr-only"
                onChange={(e) => onUpload(e.target.files)}
              />
            </label>
          </>
        )}
      </div>

      {files.length > 0 && (
        <div>
          <span className="label block">Or pick one already on disk</span>
          <div className="mt-1.5 max-h-40 overflow-y-auto rounded-card bg-surface-1">
            {files.map((file) => (
              <button
                key={file.uri}
                type="button"
                onClick={() => {
                  setUri(file.uri)
                  if (!name.trim()) setName(file.name.replace(/\.[^.]+$/, ''))
                }}
                className={`flex w-full items-center justify-between gap-3 px-3 py-2 text-left
                  text-[13px] transition-colors duration-150 ${
                    uri === file.uri ? 'bg-surface-3' : 'hover:bg-surface-2'
                  }`}
              >
                <span className="min-w-0">
                  <span className="block truncate text-ink-hi">{file.name}</span>
                  <span className="block truncate text-[11.5px] text-ink-low">{file.uri}</span>
                </span>
                <span className="shrink-0 text-right text-[11.5px] tabular-nums text-ink-low">
                  {bytes(file.bytes)}
                  {file.in_use && <span className="block text-plate-yellow">already a source</span>}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {uri && (
        <p className="break-words text-[12.5px] text-ink-mid">
          Selected <span className="text-ink-hi">{uri}</span>
        </p>
      )}
    </div>
  )
}
