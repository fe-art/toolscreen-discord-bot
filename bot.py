import logging
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
import yaml

log = logging.getLogger("toolscreen-bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ROOT = Path(__file__).resolve().parent
config = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))

BOT_TOKEN: str = config["bot_token"]
GUILD_ID: int = config["guild_id"]
WATCHED: set[int] = set(config.get("watched_channel_ids", []))
TRIAGE_DELAY: int = config.get("triage_delay_seconds", 2)
TAG_BUG: str = config.get("bug_tag_name", "Bug").lower()
TAG_ONGOING: str = config.get("ongoing_tag_name", "Ongoing").lower()
TAG_DONE: str = config.get("done_tag_name", "Done").lower()
INACTIVITY_H: int = config.get("inactivity_hours", 24)
INACTIVITY_CHECK_MIN: int = config.get("inactivity_check_interval_minutes", 30)

DEFAULT_TRIAGE = """\
Hey @MENTION, to help troubleshoot please fill in what you can:

OS: (e.g. Windows 10, Windows 11)
Toolscreen version:
Minecraft version:
Launcher + version: (e.g. MultiMC 0.7.0, Prism 8.0)
Java version: (run `java -version`)
GPU: (e.g. NVIDIA RTX 3060, AMD RX 6700 XT)
Display mode: Fullscreen / Windowed / Borderless
What happened: (steps to reproduce + what you expected vs what you got)
Full launcher log: (Edit Instance > Minecraft Log — not `latest.log`)

Optional: other mods installed, injector.log, screenshot/video, your config (`!config`)

Toolscreen requires fullscreen to work. If nothing shows up after install, try F11 first."""

CLOSE_MSG = "\u23f3 No replies in {hours}h \u2014 marking as done. Post again to reopen."

# --- DB ---

DB = ROOT / "bot.db"
_conn = sqlite3.connect(DB)
_conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
_conn.commit()


def db_get(key: str, default: str | None = None) -> str | None:
    row = _conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def db_set(key: str, value: str):
    _conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    _conn.commit()


# --- Tag helpers ---

def find_tag(channel: discord.ForumChannel, name: str) -> discord.ForumTag | None:
    return next((t for t in channel.available_tags if t.name.lower() == name), None)


def has_tag(thread: discord.Thread, name: str) -> bool:
    return any(t.name.lower() == name for t in thread.applied_tags)


async def set_tag(thread: discord.Thread, tag: discord.ForumTag) -> bool:
    if tag.id in {t.id for t in thread.applied_tags}:
        return False
    tags = (list(thread.applied_tags) + [tag])[:5]
    try:
        await thread.edit(applied_tags=tags)
        return True
    except discord.HTTPException as e:
        log.error("Tag add failed on %s: %s", thread.id, e)
        return False


async def unset_tag(thread: discord.Thread, tag: discord.ForumTag) -> bool:
    tags = [t for t in thread.applied_tags if t.id != tag.id]
    if len(tags) == len(thread.applied_tags):
        return False
    try:
        await thread.edit(applied_tags=tags)
        return True
    except discord.HTTPException as e:
        log.error("Tag remove failed on %s: %s", thread.id, e)
        return False


# --- Bot ---

intents = discord.Intents.default()
intents.guilds = True
intents.guild_messages = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
guild_obj = discord.Object(id=GUILD_ID)


@tree.command(name="bugform", description="Set the bug triage message", guild=guild_obj)
@app_commands.describe(message="New message (use \\n for newlines)")
async def cmd_bugform(interaction: discord.Interaction, message: str):
    text = message.replace("\\n", "\n")
    db_set("triage_message", text)
    log.info("Triage updated by %s", interaction.user)
    await interaction.response.send_message(f"Updated.\n>>> {text[:500]}", ephemeral=True)


@tree.command(name="bugform-reset", description="Reset bug triage to default", guild=guild_obj)
async def cmd_bugform_reset(interaction: discord.Interaction):
    db_set("triage_message", DEFAULT_TRIAGE)
    log.info("Triage reset by %s", interaction.user)
    await interaction.response.send_message("Reset to default.", ephemeral=True)


@client.event
async def on_ready():
    log.info("Online as %s", client.user)
    await tree.sync(guild=guild_obj)
    if not check_inactive.is_running():
        check_inactive.start()


@client.event
async def on_thread_create(thread: discord.Thread):
    if thread.parent_id not in WATCHED:
        return

    parent = thread.parent
    if not isinstance(parent, discord.ForumChannel):
        return

    ongoing = find_tag(parent, TAG_ONGOING)
    if ongoing:
        await set_tag(thread, ongoing)

    if not has_tag(thread, TAG_BUG):
        return

    await asyncio.sleep(TRIAGE_DELAY)
    msg = db_get("triage_message", DEFAULT_TRIAGE).replace("@MENTION", f"<@{thread.owner_id}>")
    try:
        await thread.send(msg)
    except discord.HTTPException as e:
        log.error("Triage send failed on %s: %s", thread.id, e)


@tasks.loop(minutes=INACTIVITY_CHECK_MIN)
async def check_inactive():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=INACTIVITY_H)
    guild = client.get_guild(GUILD_ID)
    if not guild:
        return

    for cid in WATCHED:
        ch = guild.get_channel(cid)
        if not isinstance(ch, discord.ForumChannel):
            continue

        done = find_tag(ch, TAG_DONE)
        ongoing = find_tag(ch, TAG_ONGOING)
        if not done:
            continue

        for thread in ch.threads:
            if has_tag(thread, TAG_DONE) or thread.archived:
                continue

            last = thread.archive_timestamp or thread.created_at
            if thread.last_message_id:
                last = datetime.fromtimestamp(
                    ((thread.last_message_id >> 22) + 1420070400000) / 1000, tz=timezone.utc
                )
            if last > cutoff:
                continue

            try:
                msgs = [m async for m in thread.history(limit=5)]
            except discord.HTTPException:
                continue
            if sum(1 for m in msgs if not m.author.bot) > 1:
                continue

            log.info("Closing inactive thread '%s' (%s)", thread.name, thread.id)
            try:
                await thread.send(CLOSE_MSG.format(hours=INACTIVITY_H))
                await set_tag(thread, done)
                if ongoing:
                    await unset_tag(thread, ongoing)
                await thread.edit(archived=True)
            except discord.HTTPException:
                pass


@check_inactive.before_loop
async def _wait():
    await client.wait_until_ready()


if __name__ == "__main__":
    client.run(BOT_TOKEN, log_handler=None)
