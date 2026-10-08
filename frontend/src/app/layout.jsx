// frontend/src/app/layout.jsx
import './globals.css'
import AppShell from '@/components/layout/AppShell'
import { INIT_SCRIPT } from '@/lib/prefs'

export const metadata = {
  title: 'Apex Job Agent',
  description: 'Autonomous job application agent',
}

// INIT_SCRIPT runs before first paint, so the saved theme and preferences are in
// effect with no flash of the default look. suppressHydrationWarning on <html> is
// because of exactly this: the attributes change before React hydrates.
export default function RootLayout({ children }) {
  return (
    <html lang="en" data-theme="graphite" suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: INIT_SCRIPT }} /></head>
      <body><AppShell>{children}</AppShell></body>
    </html>
  )
}
