// Render WooSarr's icon set from assets/brand/woosarr.svg (`make icons`).
//
// Uses Playwright's Chromium (already installed for the UI tests) so filters and
// gradients render exactly like a browser does. Small sizes (<= 64 px) use a
// tighter crop around the moon and raven; the full artwork is unreadable there.
// Playwright comes from frontend/node_modules (`cd frontend && npm ci` first).
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const { chromium } = createRequire(join(root, "frontend/package.json"))("@playwright/test");
const master = readFileSync(join(root, "assets/brand/woosarr.svg"), "utf8");
const CROPS = { full: "0 0 1024 1024", small: "112 120 800 800" };

function variant(crop) {
  return master
    .replace(/viewBox="[^"]+"/, `viewBox="${CROPS[crop]}"`)
    .replace(/ width="1024" height="1024"/, "");
}

// [output path, crop, size, type]
const RASTERS = [
  ["frontend/public/apple-touch-icon.png", "full", 180, "image/png"], // iOS home screen
  ["frontend/public/icon-192.png", "full", 192, "image/png"], // web manifest
  ["frontend/public/icon-512.png", "full", 512, "image/png"], // web manifest + Discord bot avatar download
  ["frontend/public/logo-64.webp", "small", 64, "image/webp"], // sidebar (30 px @2x)
  ["frontend/public/logo-192.webp", "full", 192, "image/webp"], // login / setup (96 px @2x)
  ["unraid/icon.png", "full", 256, "image/png"], // Unraid template / Community Apps
  ["backend/woosarr/assets/bot-avatar.png", "full", 512, "image/png"], // default Discord bot avatar
];
const ICO_SIZES = [16, 32, 48];

const browser = await chromium.launch();
const page = await browser.newPage();
await page.setContent("<!doctype html><body></body>");

async function render(crop, size, type) {
  const svg = variant(crop);
  const b64 = await page.evaluate(async ({ svg, size, type }) => {
    const img = new Image();
    img.src = "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(svg)));
    await img.decode();
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = size;
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(img, 0, 0, size, size);
    return canvas.toDataURL(type, 0.92).split(",")[1];
  }, { svg, size, type });
  return Buffer.from(b64, "base64");
}

for (const [out, crop, size, type] of RASTERS) {
  const data = await render(crop, size, type);
  writeFileSync(join(root, out), data);
  console.log(`${out} ${size}px ${(data.length / 1024).toFixed(1)} KB`);
}

// favicon.ico: PNG-compressed entries (supported by every browser that still asks for .ico).
const pngs = [];
for (const size of ICO_SIZES) pngs.push([size, await render("small", size, "image/png")]);
const header = Buffer.alloc(6 + 16 * pngs.length);
header.writeUInt16LE(0, 0);
header.writeUInt16LE(1, 2); // icon
header.writeUInt16LE(pngs.length, 4);
let offset = header.length;
pngs.forEach(([size, data], i) => {
  const e = 6 + 16 * i;
  header.writeUInt8(size, e);
  header.writeUInt8(size, e + 1);
  header.writeUInt16LE(1, e + 4); // planes
  header.writeUInt16LE(32, e + 6); // bits per pixel
  header.writeUInt32LE(data.length, e + 8);
  header.writeUInt32LE(offset, e + 12);
  offset += data.length;
});
const ico = Buffer.concat([header, ...pngs.map(([, d]) => d)]);
writeFileSync(join(root, "frontend/public/favicon.ico"), ico);
console.log(`frontend/public/favicon.ico ${ICO_SIZES.join("/")}px ${(ico.length / 1024).toFixed(1)} KB`);

// favicon.svg: the small crop, scalable for modern browsers' tabs.
const favicon = variant("small");
writeFileSync(join(root, "frontend/public/favicon.svg"), favicon);
console.log(`frontend/public/favicon.svg ${(favicon.length / 1024).toFixed(1)} KB`);

await browser.close();
