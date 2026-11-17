import styles from './Chip.module.css'

/**
 * 20px tall, 6px horizontal padding, 3px radius, 11px mono uppercase (§3.5).
 *
 * `tone` names a meaning, never a colour. The optional 5px leading dot is on
 * by default for status tones and off for the neutral and solid ones, where
 * it would be a dot meaning nothing.
 *
 * Note what is deliberately NOT a chip: a product role. A filled chip there
 * read as a button somebody was supposed to press, and a product role is a
 * fact about a person, not an action.
 */
export default function Chip({ tone = 'neutral', dot, children, title }) {
  const showDot = dot ?? !['neutral', 'solid'].includes(tone)
  return (
    <span className={`${styles.chip} ${styles[tone] ?? styles.neutral}`} title={title}>
      {showDot && <span className={styles.dot} aria-hidden="true" />}
      {children}
    </span>
  )
}
