// Stub for the @wasm/* engine modules: the harness never loads the real
// engine (game state is seeded directly, card data is mocked). This only
// satisfies module resolution for the import graph.
export default {};
export const __harnessWasmStub = true;
