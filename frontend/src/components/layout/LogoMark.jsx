// frontend/src/components/layout/LogoMark.jsx
// The app mark: an "A" peak over a baseline — the same shape as favicon.ico.
export default function LogoMark() {
  return (
    <span className="logo-mark" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
        <path d="M5 17.5 12 6l7 11.5" /><path d="M8.5 17.5h7" />
      </svg>
    </span>
  )
}
