// frontend/src/components/settings/Fold.jsx
//
// One fold of a Settings page: a plain title, a one-line summary of what is set now (so the
// page reads at a glance without opening anything), an optional "recommended" note, and the
// settings themselves once opened. Opening one writes &open=<id> to the URL.
'use client'
import { AnimatePresence, motion } from 'motion/react'
import { ChevronDown } from 'lucide-react'

export default function Fold({ id, title, summary, recommended, open, onToggle, children }) {
  return (
    <section className={`fold ${open ? 'open' : ''}`} data-fold={id}>
      <button className="fold-head" onClick={() => onToggle(open ? '' : id)} aria-expanded={open} aria-controls={`fold-${id}`}>
        <span className="fold-t"><b>{title}</b>{summary && <span className="fold-s">{summary}</span>}</span>
        {recommended && <span className="fold-rec">{recommended}</span>}
        <ChevronDown className="lucide fold-ch" aria-hidden="true" />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div id={`fold-${id}`} className="fold-body" initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0, transition: { duration: 0.15 } }} transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}>
            <div className="fold-in">{children}</div>
          </motion.div>)}
      </AnimatePresence>
    </section>
  )
}
