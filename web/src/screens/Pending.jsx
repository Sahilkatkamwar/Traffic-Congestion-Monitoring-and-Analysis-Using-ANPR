// A screen in the nav that is not built yet.
//
// It says what the screen will do and what can be done in the meantime, because
// a nav item that leads to a blank page is worse than one that explains itself.
// Nothing here is a mock: no fabricated rows, no dummy chart, no placeholder
// numbers that could be mistaken for readings.

export default function Pending({ title, does, meanwhile }) {
  return (
    <div className="grid h-full place-items-center px-6">
      <div className="max-w-md">
        <div className="label">Not built yet</div>
        <h1 className="mt-1 text-[22px] font-semibold">{title}</h1>
        <p className="mt-2 text-body text-ink-mid">{does}</p>
        {meanwhile && (
          <p className="hairline-t mt-4 pt-4 text-[13px] text-ink-low">{meanwhile}</p>
        )}
      </div>
    </div>
  )
}
