import { useId } from 'react'

// Form primitives. Labels above, hint below, no boxes around groups -- spacing
// separates them. Controls take the 8px radius; only cards take 14.

export function Field({ label, hint, error, children }) {
  return (
    <label className="block">
      <span className="label block">{label}</span>
      <span className="mt-1.5 block">{children}</span>
      {error ? (
        <span className="mt-1.5 block text-[12.5px] text-plate-red">{error}</span>
      ) : hint ? (
        <span className="mt-1.5 block text-[12.5px] text-ink-low">{hint}</span>
      ) : null}
    </label>
  )
}

const INPUT =
  'w-full rounded-control bg-surface-2 px-3 py-2 text-[14px] text-ink-hi placeholder:text-ink-low ' +
  'outline-none transition-colors duration-150 focus:bg-surface-3'

export function Input(props) {
  return <input {...props} className={`${INPUT} ${props.className || ''}`} />
}

export function Select({ children, ...props }) {
  return (
    <select {...props} className={`${INPUT} ${props.className || ''}`}>
      {children}
    </select>
  )
}

export function Button({ variant = 'ghost', className = '', ...props }) {
  const styles = {
    // Plate yellow is the accent, so it marks the one action a dialog is for.
    primary: 'bg-plate-yellow text-[#1a1400] hover:bg-[#ffd23d] disabled:bg-surface-3 disabled:text-ink-low',
    ghost: 'bg-surface-2 text-ink-hi hover:bg-surface-3 disabled:text-ink-low',
    quiet: 'text-ink-mid hover:bg-surface-2 hover:text-ink-hi',
    danger: 'bg-plate-red/15 text-plate-red hover:bg-plate-red/25',
  }
  return (
    <button
      type="button"
      {...props}
      className={`rounded-control px-3.5 py-2 text-[13px] font-semibold transition-colors
        duration-150 disabled:cursor-not-allowed ${styles[variant]} ${className}`}
    />
  )
}

export function Radio({ name, value, checked, onChange, title, detail }) {
  const id = useId()
  return (
    <div
      className={`rounded-card p-3.5 transition-colors duration-150 ${
        checked ? 'bg-surface-2' : 'bg-surface-1 hover:bg-surface-2/60'
      }`}
    >
      <label htmlFor={id} className="flex cursor-pointer items-start gap-3">
        <input
          id={id}
          type="radio"
          name={name}
          value={value}
          checked={checked}
          onChange={() => onChange(value)}
          className="mt-1 h-3.5 w-3.5 accent-[var(--plate-yellow)]"
        />
        <span>
          <span className="block text-[14px] font-semibold text-ink-hi">{title}</span>
          <span className="mt-0.5 block text-[12.5px] text-ink-mid">{detail}</span>
        </span>
      </label>
    </div>
  )
}
