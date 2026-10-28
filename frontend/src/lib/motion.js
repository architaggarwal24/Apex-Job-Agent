// frontend/src/lib/motion.js
//
// Motion in one place. `motion` (motion/react) does the spring/layout work; this
// module keeps the preferences honest: the Settings > General "Motion" option
// (system / reduce / full) drives MotionConfig's reducedMotion, so a person who
// turned motion off never sees a card slide, and the pre-paint data-motion
// attribute in globals.css covers plain CSS transitions the same way.
'use client'
import { MotionConfig, animate, useInView, useMotionValue, useReducedMotion } from 'motion/react'
import { useEffect, useRef, useState } from 'react'

export const EASE = [0.2, 0.8, 0.2, 1]
export const spring = { type: 'spring', stiffness: 420, damping: 36, mass: 0.8 }

export function Motion({ motion, children }) {
  const reduced = motion === 'reduce' ? 'always' : motion === 'full' ? 'never' : 'user'
  return <MotionConfig reducedMotion={reduced} transition={{ duration: 0.28, ease: EASE }}>{children}</MotionConfig>
}

// Entrance for a page: one orchestrated stagger, used once per page load.
export const stagger = {
  show: { transition: { staggerChildren: 0.05, delayChildren: 0.04 } },
}
export const rise = {
  hidden: { opacity: 0, y: 10 },
  show: { opacity: 1, y: 0, transition: { duration: 0.32, ease: EASE } },
}

// A number that counts to its value. Falls back to the plain number when motion
// is reduced, or when the value is not a number ("—").
export function useCountUp(value, { duration = 0.7 } = {}) {
  const reduced = useReducedMotion()
  const mv = useMotionValue(0)
  const [shown, setShown] = useState(typeof value === 'number' ? value : value)
  const first = useRef(true)
  useEffect(() => {
    if (typeof value !== 'number') { setShown(value); return undefined }
    if (reduced || !first.current) { setShown(value); mv.set(value); first.current = false; return undefined }
    first.current = false
    const ctrl = animate(mv, value, { duration, ease: EASE, onUpdate: v => setShown(Math.round(v)) })
    return () => ctrl.stop()
  }, [value, reduced, mv, duration])
  return shown
}

// True once the element has scrolled into view (for bars that grow on first sight).
export function useSeen(ref) {
  return useInView(ref, { once: true, margin: '0px 0px -10% 0px' })
}
