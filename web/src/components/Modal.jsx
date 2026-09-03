import { useEffect, useRef } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

// A dialog. Escape closes it, focus moves into it and is trapped there while it
// is open, and focus returns to whatever opened it -- a dialog you cannot leave
// by keyboard is not reachable, it is a trap.
//
// Not blurred: only panels floating over the map are. This floats over the
// page, so it takes elevation and a scrim instead.

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export default function Modal({ open, title, sub, onClose, children, footer, wide = false }) {
  const reduced = useReducedMotion()
  const panelRef = useRef(null)
  const returnTo = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    returnTo.current = document.activeElement

    const panel = panelRef.current
    const first = panel?.querySelector(FOCUSABLE)
    ;(first || panel)?.focus()

    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose?.()
        return
      }
      if (event.key !== 'Tab' || !panel) return
      const items = [...panel.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null)
      if (items.length === 0) return
      const edge = event.shiftKey ? items[0] : items[items.length - 1]
      if (document.activeElement === edge) {
        event.preventDefault()
        ;(event.shiftKey ? items[items.length - 1] : items[0]).focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      returnTo.current?.focus?.()
    }
  }, [open, onClose])

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[900] flex items-start justify-center overflow-y-auto p-4 sm:p-8"
          initial={reduced ? false : { opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={reduced ? { opacity: 1 } : { opacity: 0 }}
          transition={{ duration: 0.15 }}
        >
          <div
            className="fixed inset-0 bg-[rgba(6,9,13,0.72)]"
            onClick={onClose}
            aria-hidden
          />
          <motion.div
            ref={panelRef}
            role="dialog"
            aria-modal="true"
            aria-label={title}
            tabIndex={-1}
            initial={reduced ? false : { opacity: 0, y: 14, scale: 0.985 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduced ? { opacity: 1 } : { opacity: 0, y: 8, scale: 0.99 }}
            transition={reduced ? { duration: 0 } : { type: 'spring', stiffness: 420, damping: 34 }}
            className={`relative my-auto w-full rounded-card bg-surface-1 shadow-float outline-none
              ${wide ? 'max-w-3xl' : 'max-w-lg'}`}
          >
            <header className="flex items-start justify-between gap-4 px-6 pt-5 pb-4">
              <div>
                <h2 className="text-[19px] font-semibold leading-tight">{title}</h2>
                {sub && <p className="mt-1 text-[13px] text-ink-mid">{sub}</p>}
              </div>
              <button
                type="button"
                onClick={onClose}
                aria-label="Close"
                className="-mr-2 -mt-1 rounded-control px-2.5 py-1.5 text-[18px] leading-none
                  text-ink-low transition-colors duration-150 hover:bg-surface-2 hover:text-ink-hi"
              >
                ×
              </button>
            </header>

            <div className="hairline-t px-6 py-5">{children}</div>

            {footer && (
              <div className="hairline-t flex items-center justify-end gap-2 px-6 py-4">
                {footer}
              </div>
            )}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
