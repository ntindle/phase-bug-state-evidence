// Resolve hook: map extensionless relative TS imports (e.g. "../../constants/game")
// to their ".ts" file, so Node 24 type-stripping can load the real repo file
// without modifying it. Used only by driver_5186_mainline.mjs.
export async function resolve(specifier, context, nextResolve) {
  try {
    return await nextResolve(specifier, context);
  } catch (err) {
    if (/^[./]/.test(specifier) && !/\.[a-z0-9]+$/i.test(specifier)) {
      return await nextResolve(`${specifier}.ts`, context);
    }
    throw err;
  }
}
