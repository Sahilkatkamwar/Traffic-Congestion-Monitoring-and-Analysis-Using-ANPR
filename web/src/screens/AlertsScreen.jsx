import Pending from './Pending'

export default function AlertsScreen() {
  return (
    <Pending
      title="Alerts"
      does="Blacklist hits and impossible transitions, newest first. An impossible transition shows both crops side by side, both timestamps, the distance between the sources, and the speed that would have been required."
      meanwhile="The alerts table exists and is empty. Nothing writes to it yet, so nothing is shown."
    />
  )
}
