import fs from "node:fs";
import path from "node:path";
import { inflateSync } from "node:zlib";
import { fileURLToPath } from "node:url";

const TABLE_WIDTH = 1200;
const TABLE_HEIGHT = 600;
const COLLISION_MASK_RELATIVE_PATH = "../../sinuca/public/images/game/technical/collision_mask.png";
const POCKET_MASK_RELATIVE_PATH = "../../sinuca/public/images/game/technical/pocket_mask.png";
const ACTIVE_THRESHOLD = 128;
const COLLISION_SAMPLE_COUNT = 24;

interface BinaryMask {
  width: number;
  height: number;
  data: Uint8Array;
}

interface ResolvedCollision {
  hit: boolean;
  x: number;
  y: number;
  normalX: number;
  normalY: number;
}

let collisionMaskCache: BinaryMask | null | undefined;
let pocketMaskCache: BinaryMask | null | undefined;

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function paethPredictor(a: number, b: number, c: number) {
  const p = a + b - c;
  const pa = Math.abs(p - a);
  const pb = Math.abs(p - b);
  const pc = Math.abs(p - c);
  if (pa <= pb && pa <= pc) return a;
  if (pb <= pc) return b;
  return c;
}

function readChunk(buffer: Buffer, offset: number) {
  const length = buffer.readUInt32BE(offset);
  const type = buffer.subarray(offset + 4, offset + 8).toString("ascii");
  const dataStart = offset + 8;
  const dataEnd = dataStart + length;
  const next = dataEnd + 4;
  return { length, type, data: buffer.subarray(dataStart, dataEnd), next };
}

function parsePngMask(filePath: string): BinaryMask {
  const buffer = fs.readFileSync(filePath);
  const signature = "89504e470d0a1a0a";
  if (buffer.subarray(0, 8).toString("hex") !== signature) {
    throw new Error(`Invalid PNG signature for ${filePath}`);
  }

  let width = 0;
  let height = 0;
  let bitDepth = 0;
  let colorType = 0;
  let interlaceMethod = 0;
  const idatChunks: Buffer[] = [];
  let offset = 8;
  while (offset < buffer.length) {
    const chunk = readChunk(buffer, offset);
    offset = chunk.next;
    if (chunk.type === "IHDR") {
      width = chunk.data.readUInt32BE(0);
      height = chunk.data.readUInt32BE(4);
      bitDepth = chunk.data.readUInt8(8);
      colorType = chunk.data.readUInt8(9);
      interlaceMethod = chunk.data.readUInt8(12);
    } else if (chunk.type === "IDAT") {
      idatChunks.push(Buffer.from(chunk.data));
    } else if (chunk.type === "IEND") {
      break;
    }
  }

  if (!width || !height) throw new Error(`Missing IHDR in ${filePath}`);
  if (bitDepth !== 8) throw new Error(`Unsupported bit depth ${bitDepth} for ${filePath}`);
  if (interlaceMethod !== 0) throw new Error(`Unsupported interlace method ${interlaceMethod} for ${filePath}`);

  const channels = colorType === 0 ? 1 : colorType === 2 ? 3 : colorType === 6 ? 4 : 0;
  if (!channels) throw new Error(`Unsupported color type ${colorType} for ${filePath}`);

  const inflated = inflateSync(Buffer.concat(idatChunks));
  const rowBytes = width * channels;
  const expectedLength = height * (rowBytes + 1);
  if (inflated.length < expectedLength) {
    throw new Error(`PNG payload too short for ${filePath}`);
  }

  const raw = new Uint8Array(width * height * channels);
  let srcOffset = 0;
  let dstOffset = 0;
  for (let y = 0; y < height; y += 1) {
    const filterType = inflated[srcOffset];
    srcOffset += 1;
    for (let x = 0; x < rowBytes; x += 1) {
      const left = x >= channels ? raw[dstOffset + x - channels] : 0;
      const up = y > 0 ? raw[dstOffset + x - rowBytes] : 0;
      const upLeft = y > 0 && x >= channels ? raw[dstOffset + x - rowBytes - channels] : 0;
      const source = inflated[srcOffset + x];
      let value = source;
      switch (filterType) {
        case 0:
          value = source;
          break;
        case 1:
          value = (source + left) & 0xff;
          break;
        case 2:
          value = (source + up) & 0xff;
          break;
        case 3:
          value = (source + Math.floor((left + up) / 2)) & 0xff;
          break;
        case 4:
          value = (source + paethPredictor(left, up, upLeft)) & 0xff;
          break;
        default:
          throw new Error(`Unsupported PNG filter ${filterType} for ${filePath}`);
      }
      raw[dstOffset + x] = value;
    }
    srcOffset += rowBytes;
    dstOffset += rowBytes;
  }

  const data = new Uint8Array(width * height);
  for (let i = 0, px = 0; px < width * height; px += 1, i += channels) {
    if (channels === 1) {
      data[px] = raw[i] < ACTIVE_THRESHOLD ? 1 : 0;
      continue;
    }
    const alpha = channels === 4 ? raw[i + 3] : 255;
    if (alpha < 16) {
      data[px] = 0;
      continue;
    }
    const lum = (raw[i] + raw[i + 1] + raw[i + 2]) / 3;
    data[px] = lum < ACTIVE_THRESHOLD ? 1 : 0;
  }

  return { width, height, data };
}

function maskPath(relativePath: string) {
  const currentFile = fileURLToPath(import.meta.url);
  const currentDir = path.dirname(currentFile);
  return path.resolve(currentDir, relativePath);
}

function loadMask(relativePath: string): BinaryMask | null {
  const filePath = maskPath(relativePath);
  if (!fs.existsSync(filePath)) return null;
  try {
    return parsePngMask(filePath);
  } catch (error) {
    console.warn("[sinuca-mask-load-failed]", filePath, error);
    return null;
  }
}

function getCollisionMask() {
  if (collisionMaskCache === undefined) collisionMaskCache = loadMask(COLLISION_MASK_RELATIVE_PATH);
  return collisionMaskCache ?? null;
}

function getPocketMask() {
  if (pocketMaskCache === undefined) pocketMaskCache = loadMask(POCKET_MASK_RELATIVE_PATH);
  return pocketMaskCache ?? null;
}

function sampleMask(mask: BinaryMask, x: number, y: number) {
  const px = clamp(Math.round(x), 0, mask.width - 1);
  const py = clamp(Math.round(y), 0, mask.height - 1);
  return mask.data[py * mask.width + px] === 1;
}

export function hasTableMasks() {
  return !!getCollisionMask() && !!getPocketMask();
}

export function resolveCollisionFromMask(x: number, y: number, radius: number): ResolvedCollision | null {
  const mask = getCollisionMask();
  if (!mask) return null;
  let sampleX = x;
  let sampleY = y;
  let hit = false;
  let normalX = 0;
  let normalY = 0;

  for (let iter = 0; iter < 10; iter += 1) {
    let accX = 0;
    let accY = 0;
    let overlapCount = 0;
    for (let i = 0; i < COLLISION_SAMPLE_COUNT; i += 1) {
      const angle = (i / COLLISION_SAMPLE_COUNT) * Math.PI * 2;
      const dirX = Math.cos(angle);
      const dirY = Math.sin(angle);
      if (sampleMask(mask, sampleX + dirX * radius, sampleY + dirY * radius)) {
        accX -= dirX;
        accY -= dirY;
        overlapCount += 1;
      }
    }
    if (!sampleMask(mask, sampleX, sampleY) && overlapCount === 0) {
      if (!hit) return null;
      const normalLength = Math.hypot(normalX, normalY) || 1;
      return { hit: true, x: sampleX, y: sampleY, normalX: normalX / normalLength, normalY: normalY / normalLength };
    }
    hit = true;
    if (sampleMask(mask, sampleX, sampleY)) {
      accX += 0.001;
      overlapCount += 1;
    }
    const accLength = Math.hypot(accX, accY) || 1;
    normalX = accX / accLength;
    normalY = accY / accLength;
    const step = 1.5 + overlapCount * 0.22;
    sampleX += normalX * step;
    sampleY += normalY * step;
    sampleX = clamp(sampleX, radius, TABLE_WIDTH - radius);
    sampleY = clamp(sampleY, radius, TABLE_HEIGHT - radius);
  }

  const normalLength = Math.hypot(normalX, normalY) || 1;
  return { hit: true, x: sampleX, y: sampleY, normalX: normalX / normalLength, normalY: normalY / normalLength };
}

export function segmentHitsPocketMask(ax: number, ay: number, bx: number, by: number): boolean | null {
  const mask = getPocketMask();
  if (!mask) return null;
  const dist = Math.hypot(bx - ax, by - ay);
  const steps = Math.max(1, Math.ceil(dist / 2));
  for (let i = 0; i <= steps; i += 1) {
    const t = i / steps;
    const x = ax + (bx - ax) * t;
    const y = ay + (by - ay) * t;
    if (sampleMask(mask, x, y)) return true;
  }
  return false;
}
