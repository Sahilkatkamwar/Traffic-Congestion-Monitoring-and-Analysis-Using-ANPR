import { useEffect, useState } from 'react'
import { Link, Router, useRoute } from './lib/router'
import LiveScreen from './screens/LiveScreen'
import SourcesScreen from './screens/SourcesScreen'
import AnalyzeScreen from './screens/AnalyzeScreen'
import TraceScreen from './screens/TraceScreen'
import InsightsScreen from './screens/InsightsScreen'
import AlertsScreen from './screens/AlertsScreen'
import { getHealth } from './lib/api'

const NAV = [
  { path: '/', label: 'Live' },
  { path: '/sources', label: 'Sources' },
  { path: '/analyze', label: 'Analyze' },
  { path: '/trace', label: 'Trace' },
  { path: '/insights', label: 'Insights' },
  { path: '/alerts', label: 'Alerts' },
]

function screenFor(path) {
  if (path === '/') return <LiveScreen />
  if (path.startsWith('/sources')) return <SourcesScreen />
  if (path.startsWith('/analyze')) return <AnalyzeScreen />
  if (path.startsWith('/trace')) return <TraceScreen />
  if (path.startsWith('/insights')) return <InsightsScreen />
  if (path.startsWith('/alerts')) return <AlertsScreen />
  return null
}

// Slow enough to be invisible in the network log, quick enough that the
// header never contradicts the feed sitting next to it.
const HEALTH_REFRESH_MS = 5000

function isActive(path, current) {
  return path === '/' ? current === '/' : current.startsWith(path)
}

function Shell() {
  const { path } = useRoute()
  const [health, setHealth] = useState(null)
  const screen = screenFor(path)

  // The header counts are a global readout, shown on every screen, so they
  // refresh on a timer rather than off the live socket: opening a second
  // websocket just to keep two numbers current costs more than this poll, and
  // the counts must also stay right on Sources or Trace, where nothing is
  // listening to the feed at all. Without the interval a page opened before
  // the workers ran sat at "0 sightings" while the feed beside it filled up.
  useEffect(() => {
    let cancelled = false
    const refresh = () =>
      getHealth()
        .then((h) => !cancelled && setHealth(h))
        .catch(() => !cancelled && setHealth(null))

    refresh()
    const timer = setInterval(refresh, HEALTH_REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [path])

  return (
    <div className="flex h-full flex-col">
      {/* One hairline, no boxes. The nav is a row of type, not a set of tabs. */}
      <header className="z-[700] flex shrink-0 items-center gap-6 px-5 py-3"
        style={{ borderBottom: '1px solid var(--hairline)' }}>
        <Link
          to="/"
          className="font-plate text-[17px] font-semibold tracking-plate text-ink-hi"
        >
          ANPR<span className="text-plate-yellow">CITY</span>
        </Link>

        <nav className="flex items-center gap-1" aria-label="Main">
          {NAV.map((item) => {
            const active = isActive(item.path, path)
            return (
              <Link
                key={item.path}
                to={item.path}
                aria-current={active ? 'page' : undefined}
                className={`rounded-control px-3 py-1.5 text-[13.5px] transition-colors duration-150 ${
                  active
                    ? 'bg-surface-2 font-semibold text-ink-hi'
                    : 'text-ink-mid hover:bg-surface-1 hover:text-ink-hi'
                }`}
              >
                {item.label}
              </Link>
            )
          })}
        </nav>

        <div className="ml-auto flex items-center gap-4 text-[12px] text-ink-low">
          {health && (
            <>
              <span className="tabular-nums">
                {health.sightings} sighting{health.sightings === 1 ? '' : 's'}
              </span>
              <span className="tabular-nums">
                {health.sources} source{health.sources === 1 ? '' : 's'}
              </span>
            </>
          )}
        </div>
      </header>

      <main className="relative min-h-0 flex-1">
        {screen || (
          <div className="grid h-full place-items-center px-6">
            <div className="max-w-sm text-center">
              <h1 className="text-[20px] font-semibold">This page does not exist.</h1>
              <p className="mt-2 text-body text-ink-mid">
                Nothing is served at <code className="text-ink-hi">{path}</code>.
              </p>
              <Link
                to="/"
                className="mt-4 inline-block rounded-control bg-plate-yellow px-3.5 py-2 text-[13px] font-semibold text-[#1a1400]"
              >
                Back to Live
              </Link>
            </div>
          </div>
        )}
      </main>
    </div>
  )
}

export default function App() {
  return (
    <Router>
      <Shell />
    </Router>
  )
}
