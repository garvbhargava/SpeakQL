import Chip from './Chip.jsx'
import styles from './SealBand.module.css'

/**
 * 32px tall, sunk ground, full answer width, hairline beneath. Carries
 * route · confidence · latency · rows, right-aligned, mono (§3.5).
 *
 * The seal is what makes an answer checkable at a glance: the tone says which
 * of the five modes this is, and the right-hand figures say how it was
 * reached. A failure never gets one — there is no answer to seal — which is
 * why the retry strip exists as a separate component.
 */

const LABEL = {
  answer: 'answered',
  blocked: 'refused by the validator',
  clarify: 'asking back',
  refusal: 'cannot be answered',
  caution: 'low confidence',
  incomplete: 'short of data',
}

export default function SealBand({ tone = 'answer', route, confidence, latencyMs, rowCount, note }) {
  const facts = []
  if (route) facts.push(route)
  if (confidence !== null && confidence !== undefined)
    facts.push(`confidence ${confidence.toFixed(2)}`)
  if (latencyMs !== null && latencyMs !== undefined) facts.push(formatMs(latencyMs))
  if (rowCount !== null && rowCount !== undefined)
    facts.push(`${rowCount} ${rowCount === 1 ? 'row' : 'rows'}`)

  return (
    <div className={`${styles.seal} ${styles[tone] ?? styles.answer}`}>
      <div className={styles.left}>
        <Chip tone={chipTone(tone)}>{LABEL[tone] ?? tone}</Chip>
        {note && <span className={styles.note}>{note}</span>}
      </div>
      <div className={styles.facts}>
        {facts.map((fact, i) => (
          <span key={fact + i} className={styles.fact}>
            {fact}
          </span>
        ))}
      </div>
    </div>
  )
}

function chipTone(tone) {
  if (tone === 'answer') return 'verified'
  if (tone === 'incomplete') return 'caution'
  return tone
}

/** A person reads "1.4 s", not "1412". Sub-second stays in milliseconds
 *  because "0.09 s" is harder to compare than "89 ms". */
export function formatMs(ms) {
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(1)} s`
}
