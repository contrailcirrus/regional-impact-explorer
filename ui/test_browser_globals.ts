/**
 * @fileoverview Minimal browser-global stubs for running the dashboard's
 * production modules in a Node-based Jasmine test runner, plus a way to restore
 * the original globals.
 *
 * Importing this module as the first static `import` in a test file installs
 * the default browser global stubs at module-evaluation time (before subsequent
 * static `import` declarations are evaluated under ES module hoisting). Test
 * suites should also call `installBrowserGlobalStubs(...)` in `beforeAll` and
 * `restoreBrowserGlobals()` in `afterAll` so suites remain isolated when run in
 * a single Jasmine process.
 */

/** Selects which optional globals to stub in addition to window/document. */
export interface BrowserGlobalStubOptions {
  /**
   * Stubs `React` with `{Component: class {}, memo: fn}` for modules that
   * reference `React` at load time.
   */
  readonly react?: boolean;
  /** Stubs `d3` with an empty namespace object. */
  readonly d3?: boolean;
  /**
   * Stubs `MaterialUI` with string component tag names for modules that import
   * Material-UI components in Node tests.
   */
  readonly materialUi?: boolean;
}

/** Minimal `document` stub: canvases have no 2D context, listeners no-op. */
function createDocumentStub(): object {
  return {
    createElement: () => ({getContext: () => null}),
    addEventListener: () => {},
    removeEventListener: () => {},
  };
}

/**
 * Shared mutable `React` stub object so CommonJS shims that capture
 * `window.React` at module-import time see method overrides assigned later by
 * individual test suites.
 */
const reactStub: Record<string, unknown> = {
  'Component': class {},
  'memo': <T,>(component: T): T => component,
};

/** Shared `MaterialUI` stub object for Node-based component tests. */
const materialUiStub: Record<string, unknown> = {
  'Box': 'Box',
  'CircularProgress': 'CircularProgress',
  'IconButton': 'IconButton',
  'MenuItem': 'MenuItem',
  'Select': 'Select',
  'Typography': 'Typography',
};

function resetReactStub(): Record<string, unknown> {
  for (const key of Object.keys(reactStub)) {
    delete reactStub[key];
  }
  reactStub['Component'] = class {};
  reactStub['memo'] = <T,>(component: T): T => component;
  return reactStub;
}

/**
 * Own-property descriptors of every stubbed global as they were before the
 * first stub, or `undefined` if the property did not exist.
 */
const savedDescriptors = new Map<string, PropertyDescriptor | undefined>();

/**
 * Saves the original descriptor for `key` on `globalThis` (if not already
 * saved) and assigns `value`.
 */
function stubGlobal(key: string, value: unknown): void {
  if (!savedDescriptors.has(key)) {
    savedDescriptors.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
  }
  (globalThis as unknown as Record<string, unknown>)[key] = value;
}

/**
 * Restores `key` on `globalThis` to its original state if it was previously
 * stubbed, and removes its entry from `savedDescriptors`.
 */
function restoreGlobalKey(key: string): void {
  if (!savedDescriptors.has(key)) {
    return;
  }
  const descriptor = savedDescriptors.get(key);
  if (descriptor) {
    Object.defineProperty(globalThis, key, descriptor);
  } else {
    delete (globalThis as unknown as Record<string, unknown>)[key];
  }
  savedDescriptors.delete(key);
}

interface NodeModuleLoadResult {
  format: string;
  shortCircuit: boolean;
  source: string;
}

interface NodeModuleBuiltin {
  registerHooks?: (hooks: {
    resolve?: (
      specifier: string,
      context: unknown,
      nextResolve: (specifier: string, context: unknown) => unknown,
    ) => unknown;
    load?: (
      url: string,
      context: unknown,
      nextLoad: (url: string, context: unknown) => unknown,
    ) => NodeModuleLoadResult | unknown;
  }) => void;
}

const REACT_ESM_SHIM_SOURCE = [
  'const getReact = () => globalThis.React || {};',
  'export class Component {',
  '  constructor(props) {',
  '    this.props = props;',
  '  }',
  '}',
  "export const Fragment = 'React.Fragment';",
  'export const memo = (component) => ' +
    '(getReact().memo ? getReact().memo(component) : component);',
  'export const createElement = (...args) => getReact().createElement(...args);',
  'export const useEffect = (...args) => getReact().useEffect(...args);',
  'export const useState = (...args) => getReact().useState(...args);',
  'export const useCallback = (...args) => getReact().useCallback(...args);',
  'export const useMemo = (...args) => getReact().useMemo(...args);',
  'export const useRef = (...args) => getReact().useRef(...args);',
  'export default getReact();',
].join('\n');

const MATERIAL_UI_ESM_SHIM_SOURCE = [
  'const getMui = () => globalThis.MaterialUI || {};',
  "export const Box = getMui().Box ?? 'Box';",
  "export const CircularProgress = getMui().CircularProgress ?? 'CircularProgress';",
  "export const IconButton = getMui().IconButton ?? 'IconButton';",
  "export const MenuItem = getMui().MenuItem ?? 'MenuItem';",
  "export const Select = getMui().Select ?? 'Select';",
  "export const Typography = getMui().Typography ?? 'Typography';",
  'export default getMui();',
].join('\n');

let nodeEsmShimsRegistered = false;

/**
 * In standalone Node ES module test runs (`npm test` via `tsx`), registers
 * synchronous module loader hooks so bare `react`, `material-ui`, and
 * `@material-ui/core` imports resolve to the light test stubs on `globalThis`
 * (mirroring the global shims the in-repo build provides).
 */
function registerNodeEsmShimsIfAvailable(): void {
  if (nodeEsmShimsRegistered) {
    return;
  }
  const globalObj = globalThis as unknown as Record<string, unknown>;
  if (typeof globalObj['goog'] !== 'undefined') {
    return;
  }
  const proc = globalObj['process'] as
    | {getBuiltinModule?: (id: string) => NodeModuleBuiltin | undefined}
    | undefined;
  const moduleBuiltin = proc?.getBuiltinModule?.('node:module');
  if (typeof moduleBuiltin?.registerHooks !== 'function') {
    return;
  }
  nodeEsmShimsRegistered = true;
  moduleBuiltin.registerHooks({
    resolve(specifier, context, nextResolve) {
      if (specifier === 'react') {
        return {url: 'contrails-test-shim:react', shortCircuit: true};
      }
      if (specifier === 'material-ui' || specifier === '@material-ui/core') {
        return {url: 'contrails-test-shim:material-ui', shortCircuit: true};
      }
      return nextResolve(specifier, context);
    },
    load(url, context, nextLoad) {
      if (url === 'contrails-test-shim:react') {
        return {
          format: 'module',
          shortCircuit: true,
          source: REACT_ESM_SHIM_SOURCE,
        };
      }
      if (url === 'contrails-test-shim:material-ui') {
        return {
          format: 'module',
          shortCircuit: true,
          source: MATERIAL_UI_ESM_SHIM_SOURCE,
        };
      }
      return nextLoad(url, context);
    },
  });
}

/**
 * Replaces `window` (aliased to `globalThis`), `document`, and top-level
 * `addEventListener`/`removeEventListener` with minimal stubs, plus
 * `React`/`d3`/`MaterialUI` when requested. Unrequested optional globals are
 * restored to their pre-stub state. Re-installing is allowed; the originals
 * captured by the first call are the ones restored.
 */
export function installBrowserGlobalStubs(
  options: BrowserGlobalStubOptions = {},
): void {
  stubGlobal('window', globalThis);
  stubGlobal('document', createDocumentStub());
  stubGlobal('addEventListener', () => {});
  stubGlobal('removeEventListener', () => {});
  if (options.react) {
    stubGlobal('React', resetReactStub());
  } else {
    restoreGlobalKey('React');
  }
  if (options.d3) {
    stubGlobal('d3', {});
  } else {
    restoreGlobalKey('d3');
  }
  if (options.materialUi) {
    stubGlobal('MaterialUI', materialUiStub);
  } else {
    restoreGlobalKey('MaterialUI');
  }
}

/**
 * Restores every global replaced by `installBrowserGlobalStubs`, deleting the
 * ones that did not exist beforehand.
 */
export function restoreBrowserGlobals(): void {
  for (const key of Array.from(savedDescriptors.keys())) {
    restoreGlobalKey(key);
  }
}

// Install stubs and Node ESM loader hooks immediately when this module is
// evaluated so static imports that follow `import ... from './test_browser_globals'`
// in ES module mode see `window`, `document`, `React`, and `MaterialUI` at
// module evaluation time.
registerNodeEsmShimsIfAvailable();
installBrowserGlobalStubs({react: true, materialUi: true});
