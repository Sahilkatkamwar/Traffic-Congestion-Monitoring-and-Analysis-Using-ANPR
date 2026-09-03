import { asPercent } from '../lib/format'

// A plate is evidence, so it is set in a condensed grotesque and letter-spaced
// rather than left to look like table text. Confidence travels with it always:
// a plate string without its score is a claim without a source.

const SIZES = {
  sm: 'text-[15px] px-2 py-[3px]',
  md: 'text-[19px] px-2.5 py-1',
  lg: 'text-[26px] px-3 py-1.5',
}

export default function PlateString({ text, conf, size = 'md', className = '' }) {
  const score = asPercent(conf)

  if (!text) {
    return (
      <span
        className={`inline-flex items-center gap-2 ${className}`}
        title="This vehicle was tracked but no plate could be read. The sighting is still recorded."
      >
        <span
          className={`font-plate tracking-plate rounded-control bg-surface-3 text-ink-low ${SIZES[size]}`}
        >
          NO READ
        </span>
      </span>
    )
  }

  // Under half, the read is a lead rather than an identification, and the
  // colour says so before the number does.
  const weak = typeof conf === 'number' && conf < 0.5

  return (
    <span className={`inline-flex items-baseline gap-2 ${className}`}>
      <span
        className={`font-plate tracking-plate font-semibold rounded-control
          bg-plate-white text-[#12161c] ${SIZES[size]}`}
      >
        {text}
      </span>
      {score && (
        <span
          className={`text-[11px] tabular-nums font-semibold ${
            weak ? 'text-plate-yellow' : 'text-ink-mid'
          }`}
          title={weak ? 'Low confidence -- treat as a lead, not an identification' : 'Read confidence'}
        >
          {score}
        </span>
      )}
    </span>
  )
}
