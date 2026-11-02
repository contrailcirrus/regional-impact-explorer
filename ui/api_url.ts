/// <reference types="vite/client" />

/**
 * Origin and path prefix of the backend API, with no trailing slash, e.g.
 * `https://api.dev.contrails.org/internal/regional`.
 *
 * Set at build time by `vite.config.ts`. Empty for local builds and the dev
 * server, so requests stay same-origin and go through the dev proxy. Read
 * defensively because the unit tests run outside Vite, where `import.meta.env`
 * is undefined.
 */
export const API_BASE_URL: string = (
  (import.meta.env?.['VITE_API_BASE_URL'] as string | undefined) ?? ''
).replace(/\/+$/, '');

/**
 * Returns the URL of a backend API path.
 *
 * @param path API path starting with `/`, e.g. `/years`.
 */
export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}
