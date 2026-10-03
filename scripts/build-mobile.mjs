import { cp, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const output = path.join(root, "mobile", "www");
const apiBase = (process.env.DOOMSDAY_API_BASE || "https://doomsday-gm5k.onrender.com").replace(/\/$/, "");
const parsedApiBase = new URL(apiBase);

if (parsedApiBase.protocol !== "https:" || parsedApiBase.pathname !== "/" || parsedApiBase.search || parsedApiBase.hash) {
  throw new Error("The mobile app must use an HTTPS API URL.");
}

await mkdir(output, { recursive: true });
await cp(path.join(root, "static"), output, { recursive: true });
await writeFile(
  path.join(output, "mobile-config.js"),
  `window.DOOMSDAY_CONFIG = Object.freeze({ apiBase: ${JSON.stringify(apiBase)} });\n`,
  "utf8"
);

const htmlPath = path.join(output, "index.html");
let html = await readFile(htmlPath, "utf8");
html = html.replaceAll("https://doomsday-gm5k.onrender.com", parsedApiBase.origin);
const appScript = '  <script src="/app.js" defer></script>';
if (!html.includes(appScript)) throw new Error("Couldn't find the Doomsday app script in the mobile web bundle.");
await writeFile(
  htmlPath,
  html.replace(appScript, '  <script src="/native-bridge.js" defer></script>\n' + appScript),
  "utf8"
);

await build({
  entryPoints: [path.join(root, "mobile", "native-entry.js")],
  bundle: true,
  format: "iife",
  platform: "browser",
  target: ["safari15", "chrome100"],
  outfile: path.join(output, "native-bridge.js"),
  legalComments: "none"
});

console.log(`Prepared mobile app assets in ${path.relative(root, output)} for ${apiBase}`);
