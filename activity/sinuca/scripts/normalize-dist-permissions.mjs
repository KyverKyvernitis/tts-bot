import { chmod, lstat, readdir } from "node:fs/promises";
import path from "node:path";

const distDirectory = path.resolve(process.cwd(), "dist");

async function normalizeTree(target) {
  const info = await lstat(target);
  if (info.isSymbolicLink()) return;
  if (!info.isDirectory()) {
    await chmod(target, 0o644);
    return;
  }

  await chmod(target, 0o755);
  const entries = await readdir(target);
  await Promise.all(entries.map((entry) => normalizeTree(path.join(target, entry))));
}

await normalizeTree(distDirectory);
