import asyncio
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import discord
from discord.ext import commands

import config
from db import SettingsDB
from webserver import run_webserver, set_health_provider


print("BOT.PY INICIOU")


def _cfg(*names: str, default=None):
    for name in names:
        if hasattr(config, name):
            return getattr(config, name)
    return default


class BotLocal(commands.Bot):
    ZIP_UPDATE_CHANNEL_ID = 1490093068706386131

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        intents.voice_states = True
        intents.messages = True

        super().__init__(
            command_prefix=commands.when_mentioned_or(
                getattr(config, "BOT_PREFIX", "_"),
                getattr(config, "PREFIX", "_"),
            ),
            intents=intents,
            help_command=None,
        )

        self.started_at = datetime.now(timezone.utc)
        self.settings_db: SettingsDB | None = None
        self.health_state: dict[str, object] = {
            "status": "starting",
            "healthy": True,
            "starting": True,
            "discord_ready": False,
            "discord_closed": False,
            "guild_count": 0,
            "latency_ms": None,
            "mongo_ok": False,
            "mongo_error": None,
            "last_update": None,
        }
        self._health_task: asyncio.Task | None = None
        self._zip_update_lock = asyncio.Lock()
        self._repo_root = Path(__file__).resolve().parent
        self._update_temp_root = Path("/tmp/discord-auto-update")
        set_health_provider(self.get_health_snapshot)

    async def setup_hook(self):
        print("SETUP_HOOK INICIOU")

        mongo_uri = _cfg("MONGODB_URI", "MONGO_URI")
        mongo_db_name = _cfg("MONGODB_DB", "MONGO_DB_NAME", "MONGODB_DB_NAME", default="chat_revive")
        mongo_collection_name = _cfg("MONGODB_COLLECTION", "MONGO_COLLECTION_NAME", "MONGODB_COLLECTION_NAME", default="settings")

        if not mongo_uri:
            raise RuntimeError("Nenhuma URI do MongoDB encontrada no config.py (MONGODB_URI/MONGO_URI).")

        self.settings_db = SettingsDB(
            mongo_uri,
            mongo_db_name,
            mongo_collection_name,
        )
        await self.settings_db.init()

        print("Carregando cogs...")

        cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
        extensions = []

        for entry in sorted(os.listdir(cogs_dir)):
            if entry.startswith("_"):
                continue

            full_path = os.path.join(cogs_dir, entry)

            if entry.endswith(".py"):
                module_name = entry[:-3]
                ext = f"cogs.{module_name}"
                extensions.append(ext)
                continue

            if os.path.isdir(full_path):
                init_py = os.path.join(full_path, "__init__.py")
                if entry != "tts" and os.path.isfile(init_py):
                    extensions.append(f"cogs.{entry}")

        # TTS foi reorganizado para um pacote próprio em cogs/tts.
        extensions.extend([
            "cogs.tts.cog",
            "cogs.tts.toggle",
        ])

        for ext in extensions:
            try:
                await self.load_extension(ext)
            except Exception as e:
                print(f"[bot] falha ao carregar {ext}: {e}")
                raise

        should_sync = str(os.getenv("SYNC_SLASH_COMMANDS", "false")).strip().lower() in {"1", "true", "yes", "on"}
        allow_global_sync = str(os.getenv("SYNC_GLOBAL_SLASH_COMMANDS", "false")).strip().lower() in {"1", "true", "yes", "on"}
        if should_sync:
            health_guild_id = 927002914449424404
            guild_ids = {int(gid) for gid in (getattr(config, "GUILD_IDS", []) or []) if gid}
            guild_ids.add(health_guild_id)

            if allow_global_sync:
                synced_global = await self.tree.sync()
                print(f"[SYNC] Slash commands sincronizados globalmente: {len(synced_global)}")
                for cmd in synced_global:
                    name = getattr(cmd, "name", None) or str(cmd)
                    print(f"[SYNC][GLOBAL] /{name}")
            else:
                print("[SYNC] Sync global pulado para preservar o Entry Point da Activity do Discord.")
                print("[SYNC] Use SYNC_GLOBAL_SLASH_COMMANDS=true somente se você souber preservar manualmente o comando Launch.")

            for guild_id in sorted(guild_ids):
                guild_obj = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild_obj)
                synced_guild = await self.tree.sync(guild=guild_obj)
                print(f"[SYNC] Slash commands sincronizados na guild {guild_id}: {len(synced_guild)}")
                for cmd in synced_guild:
                    name = getattr(cmd, "name", None) or str(cmd)
                    print(f"[SYNC][GUILD {guild_id}] /{name}")
        else:
            print("[SYNC] Pulado no boot (defina SYNC_SLASH_COMMANDS=true para sincronizar no startup)")
            print("[SYNC] Observação: comandos limitados por guild, como /health, só aparecem após sync da guild correspondente.")

    def get_health_snapshot(self) -> dict[str, object]:
        snapshot = dict(self.health_state)
        uptime_seconds = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        snapshot["uptime_seconds"] = round(uptime_seconds, 2)
        ready = bool(snapshot.get("discord_ready"))
        closed = bool(snapshot.get("discord_closed"))
        mongo_ok = bool(snapshot.get("mongo_ok"))

        starting = (not ready) and uptime_seconds < 120
        healthy = (ready and not closed and mongo_ok) or starting

        snapshot["starting"] = starting
        snapshot["healthy"] = healthy
        snapshot["status"] = "starting" if starting else ("ok" if healthy else "error")

        tts_cog = self.get_cog("TTSVoice")
        if tts_cog is not None and hasattr(tts_cog, "get_tts_metrics_snapshot"):
            try:
                snapshot["tts_metrics"] = tts_cog.get_tts_metrics_snapshot()
            except Exception as e:
                snapshot["tts_metrics_error"] = str(e)
        return snapshot

    async def _health_monitor_loop(self):
        while not self.is_closed():
            mongo_ok = False
            mongo_error = None
            try:
                if self.settings_db is not None:
                    await self.settings_db.client.admin.command("ping")
                    mongo_ok = True
                else:
                    mongo_error = "settings_db not initialized"
            except Exception as e:
                mongo_error = str(e)

            latency_ms = None
            try:
                latency_ms = round(float(self.latency) * 1000, 2)
            except Exception:
                pass

            self.health_state.update({
                "discord_ready": self.is_ready(),
                "discord_closed": self.is_closed(),
                "guild_count": len(self.guilds),
                "latency_ms": latency_ms,
                "mongo_ok": mongo_ok,
                "mongo_error": mongo_error,
                "last_update": datetime.now(timezone.utc).isoformat(),
            })
            await asyncio.sleep(15)

    def _make_zip_update_embed(self, title: str, description: str, color: discord.Color) -> discord.Embed:
        embed = discord.Embed(title=title, description=description, color=color)
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    async def _send_zip_update_message(self, message: discord.Message, title: str, description: str, color: discord.Color):
        embed = self._make_zip_update_embed(title, description, color)
        await message.reply(embed=embed, mention_author=False)

    def _git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("HOME", "/home/ubuntu")
        key_path = Path("/home/ubuntu/.ssh/id_ed25519")
        if key_path.is_file():
            env.setdefault("GIT_SSH_COMMAND", f"ssh -i {key_path} -o IdentitiesOnly=yes")
        return env

    def _run_cmd(self, args: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _normalize_zip_member_parts(self, raw_name: str) -> tuple[str, ...]:
        posix = PurePosixPath(raw_name.replace("\\", "/"))
        return tuple(part for part in posix.parts if part not in ("", "."))

    def _guess_repo_name(self, origin_url: str) -> str:
        cleaned = (origin_url or "").strip().rstrip("/")
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        if "/" in cleaned:
            cleaned = cleaned.rsplit("/", 1)[-1]
        if ":" in cleaned:
            cleaned = cleaned.rsplit(":", 1)[-1]
        return cleaned.strip()

    def _pick_zip_strip_count(self, file_members: list[tuple[str, ...]], repo_name_hint: str, branch_name: str) -> int:
        if not file_members:
            return 0

        repo_root = self._repo_root.resolve()
        repo_top_names = {child.name for child in repo_root.iterdir()}
        max_strip = min(max(len(parts) - 1, 0) for parts in file_members)
        best_strip = 0
        best_score = (-1, -1, 0)

        for strip_count in range(max_strip + 1):
            mapped_members = [parts[strip_count:] for parts in file_members]
            if any(not parts for parts in mapped_members):
                continue

            exact_exists = 0
            top_level_exists = 0
            for mapped_parts in mapped_members:
                rel_path = Path(*mapped_parts)
                if (repo_root / rel_path).exists():
                    exact_exists += 1
                if mapped_parts[0] in repo_top_names:
                    top_level_exists += 1

            score = (exact_exists, top_level_exists, -strip_count)
            if score > best_score:
                best_score = score
                best_strip = strip_count

        if best_score[:2] != (0, 0):
            return best_strip

        common_first = file_members[0][0] if file_members[0] else ""
        if common_first and all(parts and parts[0] == common_first for parts in file_members):
            wrapper_names = {
                self._repo_root.name,
                repo_name_hint,
                f"{repo_name_hint}-main",
                f"{repo_name_hint}-master",
                f"{repo_name_hint}-{branch_name}",
            }
            if common_first in wrapper_names and common_first not in repo_top_names:
                return 1

        return best_strip

    def _safe_extract_patch(self, zip_path: Path, extract_dir: Path, repo_name_hint: str, branch_name: str) -> list[tuple[Path, Path]]:
        accepted: list[tuple[Path, Path]] = []
        with zipfile.ZipFile(zip_path) as zf:
            file_members: list[tuple[str, ...]] = []
            prepared_infos: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []

            for info in zf.infolist():
                raw_parts = self._normalize_zip_member_parts(info.filename)
                if not raw_parts:
                    continue
                if raw_parts[0] == "__MACOSX":
                    continue
                if raw_parts[-1] == ".DS_Store":
                    continue
                if any(part == ".." for part in raw_parts):
                    raise RuntimeError(f"Caminho inválido no ZIP: {info.filename}")

                prepared_infos.append((info, raw_parts))
                if not info.is_dir():
                    file_members.append(raw_parts)

            strip_count = self._pick_zip_strip_count(file_members, repo_name_hint, branch_name)

            for info, raw_parts in prepared_infos:
                normalized_parts = raw_parts[strip_count:]
                if not normalized_parts:
                    continue

                normalized = PurePosixPath(*normalized_parts)
                if normalized.is_absolute() or any(part == ".." for part in normalized.parts):
                    raise RuntimeError(f"Caminho inválido no ZIP: {info.filename}")

                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise RuntimeError(f"Symlink não é permitido no ZIP: {info.filename}")

                target_rel = Path(*normalized.parts)
                if info.is_dir():
                    (extract_dir / target_rel).mkdir(parents=True, exist_ok=True)
                    continue

                extract_path = extract_dir / target_rel
                extract_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, open(extract_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                accepted.append((extract_path, target_rel))

        if not accepted:
            raise RuntimeError("O ZIP não trouxe nenhum arquivo aplicável.")
        return accepted

    def _apply_patch_to_clone(self, extracted_files: list[tuple[Path, Path]], clone_dir: Path) -> list[str]:
        changed_files: list[str] = []
        for extracted_path, rel_path in extracted_files:
            destination = (clone_dir / rel_path).resolve()
            clone_root = clone_dir.resolve()
            if clone_root not in destination.parents and destination != clone_root:
                raise RuntimeError(f"Arquivo fora do repositório: {rel_path.as_posix()}")

            destination.parent.mkdir(parents=True, exist_ok=True)
            before = destination.read_bytes() if destination.exists() else None
            data = extracted_path.read_bytes()
            if before == data:
                continue
            destination.write_bytes(data)
            changed_files.append(rel_path.as_posix())
        return changed_files

    def _process_zip_update_sync(self, zip_path: Path) -> dict[str, object]:
        self._update_temp_root.mkdir(parents=True, exist_ok=True)
        env = self._git_env()
        origin_result = self._run_cmd(["git", "remote", "get-url", "origin"], self._repo_root, env=env)
        if origin_result.returncode != 0:
            raise RuntimeError(f"Não foi possível descobrir o origin do git. {origin_result.stderr.strip() or origin_result.stdout.strip()}")
        origin_url = (origin_result.stdout or "").strip()
        if not origin_url:
            raise RuntimeError("O repositório local não tem origin configurado.")

        branch_result = self._run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], self._repo_root, env=env)
        branch_name = (branch_result.stdout or "main").strip() or "main"
        if branch_result.returncode != 0 or branch_name == "HEAD":
            branch_name = "main"

        work_dir = Path(tempfile.mkdtemp(prefix="discord-auto-update-", dir=str(self._update_temp_root)))
        extract_dir = work_dir / "extracted"
        clone_dir = work_dir / "clone"
        extract_dir.mkdir(parents=True, exist_ok=True)

        try:
            repo_name_hint = self._guess_repo_name(origin_url)
            extracted_files = self._safe_extract_patch(zip_path, extract_dir, repo_name_hint, branch_name)

            clone_result = self._run_cmd(["git", "clone", "--branch", branch_name, "--single-branch", origin_url, str(clone_dir)], work_dir, env=env)
            if clone_result.returncode != 0:
                err = (clone_result.stderr or clone_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao clonar o repositório temporário. {err}")

            changed_files = self._apply_patch_to_clone(extracted_files, clone_dir)
            if not changed_files:
                return {
                    "changed_files": [],
                    "commit_hash": None,
                    "triggered_update": False,
                    "branch": branch_name,
                }

            self._run_cmd(["git", "config", "user.name", "Discord Auto Update"], clone_dir, env=env)
            self._run_cmd(["git", "config", "user.email", "discord-auto-update@local"], clone_dir, env=env)

            add_result = self._run_cmd(["git", "add", "--", *changed_files], clone_dir, env=env)
            if add_result.returncode != 0:
                err = (add_result.stderr or add_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao preparar arquivos para commit. {err}")

            status_result = self._run_cmd(["git", "status", "--porcelain"], clone_dir, env=env)
            if status_result.returncode != 0:
                err = (status_result.stderr or status_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao verificar alterações do clone temporário. {err}")
            if not (status_result.stdout or "").strip():
                return {
                    "changed_files": [],
                    "commit_hash": None,
                    "triggered_update": False,
                    "branch": branch_name,
                }

            commit_message = f"auto update from discord zip ({len(changed_files)} arquivo(s))"
            commit_result = self._run_cmd(["git", "commit", "-m", commit_message], clone_dir, env=env)
            if commit_result.returncode != 0:
                err = (commit_result.stderr or commit_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao criar commit do update automático. {err}")

            push_result = self._run_cmd(["git", "push", "origin", branch_name], clone_dir, env=env)
            if push_result.returncode != 0:
                err = (push_result.stderr or push_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao enviar update para o GitHub. {err}")

            hash_result = self._run_cmd(["git", "rev-parse", "HEAD"], clone_dir, env=env)
            commit_hash = (hash_result.stdout or "").strip() if hash_result.returncode == 0 else None

            triggered_update = False
            update_script = self._repo_root / "update-bot.sh"
            if update_script.exists():
                subprocess.Popen(
                    [str(update_script)],
                    cwd=str(self._repo_root),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                triggered_update = True

            return {
                "changed_files": changed_files,
                "commit_hash": commit_hash,
                "triggered_update": triggered_update,
                "branch": branch_name,
            }
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _handle_zip_update_message(self, message: discord.Message):
        zip_attachment = None
        for attachment in message.attachments:
            if attachment.filename.lower().endswith(".zip"):
                zip_attachment = attachment
                break

        if zip_attachment is None:
            await self._send_zip_update_message(
                message,
                "❌ Arquivo inválido",
                "Envie um arquivo **.zip** neste canal para iniciar a atualização automática do projeto.",
                discord.Color.red(),
            )
            return

        if self._zip_update_lock.locked():
            await self._send_zip_update_message(
                message,
                "⏳ Atualização em andamento",
                "Já existe outra atualização automática processando um ZIP neste momento. Aguarde ela terminar e envie novamente.",
                discord.Color.orange(),
            )
            return

        async with self._zip_update_lock:
            self._update_temp_root.mkdir(parents=True, exist_ok=True)
            work_dir = Path(tempfile.mkdtemp(prefix="discord-auto-update-msg-", dir=str(self._update_temp_root)))
            zip_path = work_dir / zip_attachment.filename
            try:
                await zip_attachment.save(zip_path)
                await self._send_zip_update_message(
                    message,
                    "📦 ZIP recebido",
                    "Arquivo baixado fora do repositório. Vou validar, aplicar em clone temporário, enviar para o GitHub e acionar o auto update existente.",
                    discord.Color.blurple(),
                )

                result = await asyncio.to_thread(self._process_zip_update_sync, zip_path)
                changed_files = list(result.get("changed_files") or [])
                commit_hash = result.get("commit_hash")
                branch = result.get("branch") or "main"
                triggered_update = bool(result.get("triggered_update"))

                if not changed_files:
                    await self._send_zip_update_message(
                        message,
                        "ℹ️ Nenhuma alteração aplicada",
                        "O ZIP foi válido, mas não mudou nenhum arquivo do repositório. Nada foi commitado no GitHub.",
                        discord.Color.gold(),
                    )
                    return

                preview_files = "\n".join(f"• `{path}`" for path in changed_files[:10])
                if len(changed_files) > 10:
                    preview_files += f"\n• ... e mais {len(changed_files) - 10} arquivo(s)"
                short_hash = str(commit_hash)[:7] if commit_hash else "desconhecido"
                update_line = "O script `update-bot.sh` foi acionado." if triggered_update else "O commit foi enviado, mas o acionamento automático do script não foi encontrado."
                await self._send_zip_update_message(
                    message,
                    "✅ Update enviado para o GitHub",
                    f"Branch: **{branch}**\nCommit: **{short_hash}**\nArquivos alterados: **{len(changed_files)}**\n\n{preview_files}\n\n{update_line}",
                    discord.Color.green(),
                )
            except zipfile.BadZipFile:
                await self._send_zip_update_message(
                    message,
                    "❌ ZIP inválido",
                    "O arquivo enviado não pôde ser aberto como ZIP válido. Nenhuma alteração foi aplicada.",
                    discord.Color.red(),
                )
            except Exception as e:
                print(f"[zip_update] falha: {e!r}")
                await self._send_zip_update_message(
                    message,
                    "❌ Falha no update automático",
                    f"Nada foi aplicado. Motivo: **{e}**",
                    discord.Color.red(),
                )
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)

    async def on_ready(self):
        print(f"Logado como {self.user} (id: {self.user.id})")
        print(f"Em {len(self.guilds)} servidor(es)")
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name="/help | _help",
                )
            )
        except Exception as e:
            print(f"[bot] falha ao aplicar presence: {e!r}")
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_monitor_loop())

    async def on_message(self, message: discord.Message):
        if getattr(message.author, "bot", False):
            return
        try:
            if int(getattr(message.channel, "id", 0)) == self.ZIP_UPDATE_CHANNEL_ID:
                await self._handle_zip_update_message(message)
                return
            await self.process_commands(message)
        except Exception as e:
            print(f"[bot] falha ao processar comandos: {e!r}")

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ):
        print(f"[APP_COMMAND_ERROR] {error!r}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    f"Erro ao executar o comando: {error}",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"Erro ao executar o comando: {error}",
                    ephemeral=True,
                )
        except Exception as e:
            print(f"[APP_COMMAND_ERROR] Falha ao responder ao usuário: {e!r}")


async def main():
    print("MAIN INICIOU")

    web_thread = threading.Thread(target=run_webserver, daemon=True)
    web_thread.start()

    bot = BotLocal()
    await bot.start(config.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
