import { createContext, useCallback, useContext, useEffect, useState } from 'react'

// A router, not a routing library. Six flat routes and one optional path
// segment do not justify a dependency, and pushState plus popstate is the whole
// mechanism. FastAPI's SPA fallback is what makes these URLs survive a reload.

const RouteContext = createContext({ path: '/', navigate: () => {} })

export function Router({ children }) {
  const [path, setPath] = useState(() => window.location.pathname)

  useEffect(() => {
    const onPop = () => setPath(window.location.pathname)
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const navigate = useCallback((to, { replace = false } = {}) => {
    if (to === window.location.pathname) return
    window.history[replace ? 'replaceState' : 'pushState'](null, '', to)
    setPath(to)
  }, [])

  return (
    <RouteContext.Provider value={{ path, navigate }}>
      {children}
    </RouteContext.Provider>
  )
}

export const useRoute = () => useContext(RouteContext)

export function Link({ to, className, children, ...rest }) {
  const { navigate } = useRoute()
  return (
    <a
      href={to}
      className={className}
      onClick={(event) => {
        // Leave modified clicks alone: ctrl-click means "new tab" and always has.
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return
        event.preventDefault()
        navigate(to)
      }}
      {...rest}
    >
      {children}
    </a>
  )
}
