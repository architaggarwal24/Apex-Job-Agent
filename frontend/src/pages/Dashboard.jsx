// frontend/src/pages/Dashboard.jsx
//
// Left over from the old Vite build (src/main.jsx + src/App.jsx), never part of the Next app,
// which lives in src/app/ (/ replaces this screen). Next.js reads src/pages/ as Pages Router
// routes, so the react-router code that was here broke `npm run build`. This stub is never
// exported (notFound), so it can't clash with the real page on a case-insensitive disk.
export function getStaticProps() {
  return { notFound: true }
}

export default function Retired() {
  return null
}
