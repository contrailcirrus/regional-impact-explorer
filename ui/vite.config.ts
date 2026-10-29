import react from '@vitejs/plugin-react';
import {defineConfig} from 'vite';

export default defineConfig({
  // The sources use the classic runtime (`@jsx React.createElement` pragmas,
  // `"jsx": "react"` in `tsconfig.json`); the plugin defaults to "automatic".
  plugins: [react({jsxRuntime: 'classic'})],

  /**
   * Maps the bare specifier `'material-ui'` used in the TypeScript sources to
   * `@material-ui/core`. Keep in sync with `compilerOptions.paths` in
   * `tsconfig.json`: that entry satisfies `tsc`, this one satisfies the dev
   * server and the bundler.
   *
   * The regex is anchored so it cannot prefix-match other `material-ui*`
   * specifiers.
   */
  resolve: {
    alias: [{find: /^material-ui$/, replacement: '@material-ui/core'}],
  },

  server: {
    port: 3000,
    // Every backend route the frontend actually calls, verified against
    // `server/server.py`. Defaults to `http://localhost:9090` (matching the
    // `PORT` default in `server/server.py`) and can be overridden with
    // `CONTRAILS_BACKEND_ORIGIN`.
    //
    // Keys are path prefixes. Year-scoped routes are `/{resource}/{year}...`,
    // so `/countries/` covers both `/countries/{year}` and
    // `/countries/{year}.json` (likewise `/firs/`). The trailing slashes keep
    // `/country/` from also matching `/countries/`, and keep the prefixes off
    // the dev server's own modules: without one, `/temporal` would send
    // `/temporal_heatmap_model.ts` to the backend. `/years` is the only route
    // with no sub-path, so it is matched exactly.
    proxy: {
      '/country/':
        process.env['CONTRAILS_BACKEND_ORIGIN'] ?? 'http://localhost:9090',
      '/countries/':
        process.env['CONTRAILS_BACKEND_ORIGIN'] ?? 'http://localhost:9090',
      '/firs/':
        process.env['CONTRAILS_BACKEND_ORIGIN'] ?? 'http://localhost:9090',
      '/grid/':
        process.env['CONTRAILS_BACKEND_ORIGIN'] ?? 'http://localhost:9090',
      '/rankings/':
        process.env['CONTRAILS_BACKEND_ORIGIN'] ?? 'http://localhost:9090',
      '/series/':
        process.env['CONTRAILS_BACKEND_ORIGIN'] ?? 'http://localhost:9090',
      '/temporal/':
        process.env['CONTRAILS_BACKEND_ORIGIN'] ?? 'http://localhost:9090',
      '^/years(\\?.*)?$':
        process.env['CONTRAILS_BACKEND_ORIGIN'] ?? 'http://localhost:9090',
    },
  },

  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
