// Сборка main и preload Electron (esbuild → CommonJS в dist-electron/).
import { build, context } from "esbuild";

const watch = process.argv.includes("--watch");
const common = {
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node22",
  external: ["electron"],
  sourcemap: true,
  logLevel: "info",
};
const entries = [
  { entryPoints: ["electron/main.ts"], outfile: "dist-electron/main.cjs" },
  { entryPoints: ["electron/preload.ts"], outfile: "dist-electron/preload.cjs" },
];
if (watch) {
  for (const e of entries) {
    const ctx = await context({ ...common, ...e });
    await ctx.rebuild();
    ctx.watch();
  }
} else {
  await Promise.all(entries.map((e) => build({ ...common, ...e })));
}
