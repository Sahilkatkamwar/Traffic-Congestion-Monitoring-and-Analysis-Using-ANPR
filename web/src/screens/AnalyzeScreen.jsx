import Pending from './Pending'

export default function AnalyzeScreen() {
  return (
    <Pending
      title="Analyze"
      does="Drop in an image or a video and get annotated detections back: boxes, plate reads with confidence, vehicle types, and cropped evidence, exportable as JSON or CSV."
      meanwhile="This screen will work with no camera configured at all."
    />
  )
}
