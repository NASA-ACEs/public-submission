import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { FILM, FILM_LAYOUT, LINES_FILM } from './demo'
import Film from './Film.tsx'
import FilmLines from './FilmLines.tsx'
import FilmSplit from './FilmSplit.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {LINES_FILM ? (
      <FilmLines film={LINES_FILM} />
    ) : FILM ? (
      FILM_LAYOUT === 'sequence' ? <Film film={FILM} /> : <FilmSplit film={FILM} />
    ) : (
      <App />
    )}
  </StrictMode>,
)
