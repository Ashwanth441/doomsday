import { access, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const resources = path.join(root, "resources");
const icon = await readFile(path.join(resources, "icon-only.png"));
const foreground = await readFile(path.join(resources, "icon-foreground.png"));
const splash = await readFile(path.join(resources, "splash.png"));
const densitySizes = { mdpi: 48, hdpi: 72, xhdpi: 96, xxhdpi: 144, xxxhdpi: 192 };
const foregroundSizes = { mdpi: 108, hdpi: 162, xhdpi: 216, xxhdpi: 324, xxxhdpi: 432 };
const androidRes = path.join(root, "android", "app", "src", "main", "res");

try {
  await access(androidRes);
  for (const [density, size] of Object.entries(densitySizes)) {
    const folder = path.join(androidRes, `mipmap-${density}`);
    await writeFile(path.join(folder, "ic_launcher.png"), await sharp(icon).resize(size, size).png().toBuffer());
    await writeFile(path.join(folder, "ic_launcher_round.png"), await sharp(icon).resize(size, size).png().toBuffer());
    const fgSize = foregroundSizes[density];
    await writeFile(path.join(folder, "ic_launcher_foreground.png"), await sharp(foreground).resize(fgSize, fgSize).png().toBuffer());
  }
  await writeFile(path.join(androidRes, "values", "ic_launcher_background.xml"), `<?xml version="1.0" encoding="utf-8"?>\n<resources>\n    <color name="ic_launcher_background">#e6563d</color>\n</resources>\n`);

  for (const folderName of ["drawable", "drawable-port-mdpi", "drawable-port-hdpi", "drawable-port-xhdpi", "drawable-port-xxhdpi", "drawable-port-xxxhdpi", "drawable-land-mdpi", "drawable-land-hdpi", "drawable-land-xhdpi", "drawable-land-xxhdpi", "drawable-land-xxxhdpi"]) {
    const folder = path.join(androidRes, folderName);
    try { await access(folder); } catch { continue; }
    const targetPath = path.join(folder, "splash.png");
    let metadata;
    try { metadata = await sharp(targetPath).metadata(); } catch { metadata = { width: 480, height: 800 }; }
    await writeFile(targetPath, await sharp(splash).resize(metadata.width || 480, metadata.height || 800, { fit: "contain", background: { r: 246, g: 245, b: 241, alpha: 1 } }).png().toBuffer());
  }
} catch {
  console.log("Android project not added yet; skipped Android branding.");
}

const iosAssets = path.join(root, "ios", "App", "App", "Assets.xcassets");
try {
  await access(iosAssets);
  const iconPath = path.join(iosAssets, "AppIcon.appiconset", "AppIcon-512@2x.png");
  await writeFile(iconPath, await sharp(icon).resize(1024, 1024).png().toBuffer());
  const splashFolder = path.join(iosAssets, "Splash.imageset");
  for (const name of ["splash-2732x2732-2.png", "splash-2732x2732-1.png", "splash-2732x2732.png"]) {
    await writeFile(path.join(splashFolder, name), splash);
  }
} catch {
  console.log("iOS project not added yet; skipped iOS branding.");
}

console.log("Updated native app icons and splash screens.");
