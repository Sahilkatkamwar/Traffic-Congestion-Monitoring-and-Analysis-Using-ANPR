// An empty state says what to do next. "No data" is not a state, it is a shrug.
export default function Empty({ title, action, icon = null }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      {icon}
      <p className="text-[15px] font-semibold text-ink-hi">{title}</p>
      {action && <p className="max-w-[34ch] text-[13px] text-ink-mid">{action}</p>}
    </div>
  )
}
