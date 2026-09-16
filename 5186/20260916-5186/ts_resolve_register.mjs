// Registers the extensionless-TS resolve hook for driver_5186_mainline.mjs.
import { register } from "node:module";
register("./ts_resolve_loader.mjs", import.meta.url);
