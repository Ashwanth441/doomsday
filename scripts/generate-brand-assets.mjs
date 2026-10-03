import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const resources = path.join(root, "resources");
const source = await readFile(path.join(root, "static", "icon.svg"), "utf8");
await mkdir(resources, { recursive: true });

await sharp(Buffer.from(source)).resize(1024, 1024).png().toFile(path.join(resources, "icon-only.png"));
const foreground = source.replace(/<rect[^>]*\/>/, "");
await sharp(Buffer.from(foreground)).resize(768, 768, { fit: "contain" }).extend({
  top: 128,
  bottom: 128,
  left: 128,
  right: 128,
  background: { r: 0, g: 0, b: 0, alpha: 0 }
}).png().toFile(path.join(resources, "icon-foreground.png"));

const splashSize = 2732;
const background = Buffer.from(`<svg xmlns="http://www.w3.org/2000/svg" width="${splashSize}" height="${splashSize}"><rect width="100%" height="100%" fill="#f6f5f1"/></svg>`);
const mark = await sharp(Buffer.from(source)).resize(512, 512).png().toBuffer();
await sharp(background).composite([{ input: mark, gravity: "centre" }]).png().toFile(path.join(resources, "splash.png"));

const redBackground = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024"><rect width="100%" height="100%" fill="#e6563d"/></svg>');
await sharp(redBackground).png().toFile(path.join(resources, "icon-background.png"));
console.log("Generated Doomsday app icon and splash art in resources/.");
