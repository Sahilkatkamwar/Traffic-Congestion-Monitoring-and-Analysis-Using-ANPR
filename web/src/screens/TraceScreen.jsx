import Pending from './Pending'

export default function TraceScreen() {
  return (
    <Pending
      title="Trace"
      does="Search a plate and get a ranked list of candidates with match scores, then follow one vehicle across sources: its path on the map, a time scrubber, and the evidence behind every stop."
      meanwhile="Matching is already fuzzy end to end, so a search will never return one silent answer."
    />
  )
}
