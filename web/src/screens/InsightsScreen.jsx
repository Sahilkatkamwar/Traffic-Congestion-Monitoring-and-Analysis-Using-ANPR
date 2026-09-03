import Pending from './Pending'

export default function InsightsScreen() {
  return (
    <Pending
      title="Insights"
      does="Heatmap, vehicle counts over time, type distribution, per-source density, and origin-destination flows, all under one shared time filter."
      meanwhile="These need sources placed on the map before they can say anything about where traffic goes."
    />
  )
}
