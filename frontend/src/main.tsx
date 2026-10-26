import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Theme } from '@radix-ui/themes'
import '@radix-ui/themes/styles.css'
import './index.css'
import App from './App.tsx'

// ponytail: follows the OS theme at load; add a toggle if anyone asks.
const appearance = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Theme appearance={appearance} accentColor="jade" grayColor="sage" radius="medium">
      <App />
    </Theme>
  </StrictMode>,
)
