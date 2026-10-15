import 'jasmine';
import {
  installBrowserGlobalStubs,
  restoreBrowserGlobals,
} from './test_browser_globals';

const globalEnv = globalThis as unknown as Record<string, unknown>;

describe('test_browser_globals', () => {
  afterEach(() => {
    restoreBrowserGlobals();
  });

  it('stubs window and document without touching React or d3 by default', () => {
    installBrowserGlobalStubs();

    expect(globalEnv['window']).toBe(globalThis);
    expect(globalEnv['document']).toEqual({
      createElement: jasmine.any(Function),
      addEventListener: jasmine.any(Function),
      removeEventListener: jasmine.any(Function),
    });
    expect(globalEnv['addEventListener']).toEqual(jasmine.any(Function));
    expect(globalEnv['removeEventListener']).toEqual(jasmine.any(Function));
    expect('React' in globalThis).toBeFalse();
    expect('d3' in globalThis).toBeFalse();
    expect('MaterialUI' in globalThis).toBeFalse();
  });

  it('stubs React, d3, and MaterialUI when requested', () => {
    installBrowserGlobalStubs({react: true, d3: true, materialUi: true});

    expect(globalEnv['React']).toEqual({
      'Component': jasmine.any(Function),
      'memo': jasmine.any(Function),
    });
    expect(globalEnv['d3']).toEqual({});
    expect(globalEnv['MaterialUI']).toEqual(
      jasmine.objectContaining({
        'Box': 'Box',
        'CircularProgress': 'CircularProgress',
        'IconButton': 'IconButton',
      }),
    );
  });

  it('restores unrequested optional globals when re-installed with narrower options', () => {
    installBrowserGlobalStubs({react: true, d3: true, materialUi: true});
    installBrowserGlobalStubs({react: true});

    expect('React' in globalThis).toBeTrue();
    expect('d3' in globalThis).toBeFalse();
    expect('MaterialUI' in globalThis).toBeFalse();
  });

  it('deletes globals that did not exist before stubbing', () => {
    installBrowserGlobalStubs({react: true, d3: true, materialUi: true});

    restoreBrowserGlobals();

    expect('window' in globalThis).toBeFalse();
    expect('document' in globalThis).toBeFalse();
    expect('addEventListener' in globalThis).toBeFalse();
    expect('removeEventListener' in globalThis).toBeFalse();
    expect('React' in globalThis).toBeFalse();
    expect('d3' in globalThis).toBeFalse();
    expect('MaterialUI' in globalThis).toBeFalse();
  });

  it('restores the original value of a pre-existing global', () => {
    const originalD3 = {version: 'real'};
    globalEnv['d3'] = originalD3;

    installBrowserGlobalStubs({d3: true});
    installBrowserGlobalStubs({d3: true});
    restoreBrowserGlobals();

    expect(globalEnv['d3']).toBe(originalD3);
    delete globalEnv['d3'];
  });
});
