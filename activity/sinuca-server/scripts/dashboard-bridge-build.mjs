import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const bridgeDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoDir = resolve(bridgeDir, "../..");
const targetDir = resolve(repoDir, "dashboard/backend");
const envTemplates = new Set([".env.example", ".env.sample", ".env.template"]);

function hydrateCanonicalTree() {
  const generated = new Set(["node_modules", "dist", ".vite", ".cache", "coverage"]);
  const skipFiles = new Set(["package.json", "scripts/dashboard-bridge-build.mjs"]);
  const copyMissing = (sourceDir, destinationDir, relative = "") => {
    mkdirSync(destinationDir, { recursive: true });
    for (const entry of readdirSync(sourceDir)) {
      const rel = relative ? `${relative}/${entry}` : entry;
      if (generated.has(entry) || skipFiles.has(rel)) continue;
      const source = resolve(sourceDir, entry);
      const destination = resolve(destinationDir, entry);
      const stat = statSync(source);
      if (stat.isDirectory()) { copyMissing(source, destination, rel); continue; }
      if (!stat.isFile() || existsSync(destination)) continue;
      cpSync(source, destination, { preserveTimestamps: true });
    }
  };
  copyMissing(bridgeDir, targetDir);
  if (!existsSync(resolve(targetDir, "package-lock.json"))) throw new Error("package-lock canônico não pôde ser hidratado");
}

function stageCanonicalTree() {
  if (!existsSync(resolve(repoDir, ".git"))) return;
  const result = spawnSync("git", ["-C", repoDir, "add", "-A", "--", "dashboard/backend"], { stdio: "inherit", shell: false });
  if (result.status !== 0) throw new Error("não foi possível stagear a árvore canônica do dashboard");
}

function syncLocalEnv() {
  for (const name of readdirSync(bridgeDir)) {
    if (!(name === ".env" || name.startsWith(".env.")) || envTemplates.has(name)) continue;
    const source = resolve(bridgeDir, name);
    const target = resolve(targetDir, name);
    if (existsSync(target) && !readFileSync(source).equals(readFileSync(target))) {
      throw new Error(`conflito em configuração local do dashboard: ${name}`);
    }
    if (!existsSync(target)) cpSync(source, target, { preserveTimestamps: true });
  }
}

function run(...args) {
  const result = spawnSync("npm", args, { cwd: targetDir, stdio: "inherit", shell: false });
  if (result.status !== 0) process.exit(result.status ?? 1);
}

if (!existsSync(resolve(targetDir, "package.json"))) throw new Error(`backend canônico não encontrado: ${targetDir}`);
hydrateCanonicalTree();
syncLocalEnv();
stageCanonicalTree();
if (process.env.DASHBOARD_BRIDGE_HYDRATE_ONLY === "1") process.exit(0);
run("ci");
run("test");
run("run", "build");
run("prune", "--omit=dev");
