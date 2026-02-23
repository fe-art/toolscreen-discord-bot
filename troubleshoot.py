"""Interactive /troubleshoot command backed by troubleshooting-tree.yaml."""

import logging
import re
import sqlite3
from pathlib import Path

import discord
from discord import app_commands

log = logging.getLogger("toolscreen-bot")

ROOT = Path(__file__).resolve().parent
YAML_PATH = ROOT / "troubleshooting-tree.yaml"
BUGS_PATH = ROOT / "known-bugs.yaml"

_nodes: dict[str, dict] = {}
_bugs: dict[str, dict] = {}
_bug_by_node: dict[str, dict] = {}

_db = sqlite3.connect(ROOT / "bot.db")
_db.execute("CREATE TABLE IF NOT EXISTS node_hits (node_id TEXT PRIMARY KEY, hits INTEGER NOT NULL DEFAULT 0)")
_db.commit()


def _hit(node_id: str):
    _db.execute(
        "INSERT INTO node_hits (node_id, hits) VALUES (?, 1) ON CONFLICT(node_id) DO UPDATE SET hits = hits + 1",
        (node_id,),
    )
    _db.commit()


def top_hits(limit: int = 20) -> list[tuple[str, int]]:
    return _db.execute("SELECT node_id, hits FROM node_hits ORDER BY hits DESC LIMIT ?", (limit,)).fetchall()

CLR_QUESTION = discord.Colour.blurple()
CLR_SOLUTION = discord.Colour.green()
CLR_INFO = discord.Colour.gold()
CLR_ESCALATE = discord.Colour.orange()
CLR_DONE = discord.Colour(0x2b2d31)

SELECT_THRESHOLD = 5


def load_tree() -> int:
    """Parse the YAML files into _nodes and _bugs. Returns node count."""
    import yaml

    with open(YAML_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    _nodes.clear()
    _nodes.update(data["nodes"])
    _validate_tree()

    _bugs.clear()
    _bug_by_node.clear()
    if BUGS_PATH.exists():
        with open(BUGS_PATH, encoding="utf-8") as f:
            bug_data = yaml.safe_load(f)
        _bugs.update(bug_data.get("bugs", {}))
        for bid, bug in _bugs.items():
            if bug.get("status") != "fixed":
                for nid in bug.get("affects", []):
                    _bug_by_node[nid] = bug
        log.info("Known bugs loaded: %d (%d active)", len(_bugs),
                 sum(1 for b in _bugs.values() if b.get("status") != "fixed"))

    log.info("Troubleshoot tree loaded: %d nodes", len(_nodes))
    return len(_nodes)


def _validate_tree():
    ids = set(_nodes.keys())
    for nid, node in _nodes.items():
        for opt in node.get("options", []):
            ref = opt.get("next")
            if ref and ref not in ids:
                raise ValueError(f"Node {nid!r} references missing node {ref!r}")
        ref = node.get("did_not_help")
        if ref and ref not in ids:
            raise ValueError(f"Node {nid!r} did_not_help references missing {ref!r}")
        if len(f"ts:{nid}:solved") > 100:
            raise ValueError(f"Node id {nid!r} too long for custom_id")


def _embed_for(node: dict) -> discord.Embed:
    ntype = node["type"]
    text = node.get("text", "").strip()

    titles = {
        "question": ("Troubleshooting", CLR_QUESTION),
        "solution": ("Possible fix", CLR_SOLUTION),
        "info": ("Info", CLR_INFO),
        "escalate": ("Further help needed", CLR_ESCALATE),
    }
    title, colour = titles.get(ntype, ("Troubleshooting", CLR_QUESTION))
    return discord.Embed(title=title, description=text, colour=colour)


def render_node(node_id: str) -> tuple[discord.Embed, discord.ui.View]:
    node = _nodes.get(node_id)
    if not node:
        return _error_embed(f"Unknown node `{node_id}`"), discord.ui.View()

    ntype = node["type"]
    embed = _embed_for(node)
    view = discord.ui.View(timeout=None)

    bug = _bug_by_node.get(node_id)
    if ntype == "solution" and bug:
        tid = bug["discord_thread"]
        name = bug["name"]
        embed.add_field(
            name="Known issue",
            value=f"[{name}]({_thread_url(tid)}) - post there to get notified on updates.",
            inline=False,
        )

    if ntype == "question":
        options = node.get("options", [])
        if len(options) > SELECT_THRESHOLD:
            view.add_item(TroubleshootSelect(node_id, options))
        else:
            seen: dict[str, int] = {}
            for i, opt in enumerate(options):
                label = opt["label"][:80]
                target = opt["next"]
                count = seen.get(target, 0)
                seen[target] = count + 1
                cid = f"ts:{target}" if count == 0 else f"ts:{target}:{count}"
                view.add_item(discord.ui.Button(
                    label=label, custom_id=cid,
                    style=discord.ButtonStyle.primary, row=i // 5,
                ))

    elif ntype == "solution":
        view.add_item(discord.ui.Button(
            label="That solved it!", custom_id=f"ts:{node_id}:solved",
            style=discord.ButtonStyle.success, emoji="\u2705",
        ))
        did_not = node.get("did_not_help")
        if did_not:
            view.add_item(discord.ui.Button(
                label="Still having issues", custom_id=f"ts:{did_not}",
                style=discord.ButtonStyle.secondary,
            ))

    elif ntype == "info":
        nxt = node.get("next")
        if nxt:
            view.add_item(discord.ui.Button(
                label="Continue", custom_id=f"ts:{nxt}",
                style=discord.ButtonStyle.primary,
            ))

    elif ntype == "escalate":
        _add_escalate_content(embed, node, node_id)
        view.add_item(discord.ui.Button(
            label="That solved it!", custom_id=f"ts:{node_id}:solved",
            style=discord.ButtonStyle.success, emoji="\u2705",
        ))

    view.add_item(discord.ui.Button(
        label="Start over", custom_id="ts:root",
        style=discord.ButtonStyle.secondary, emoji="\U0001f504", row=4,
    ))

    return embed, view


GUILD_ID = "1472102343381352539"
MORE_HELP_ID = "1472103482201997499"
MORE_HELP_URL = f"https://discord.com/channels/{GUILD_ID}/{MORE_HELP_ID}"


def _thread_url(thread_id: str) -> str:
    return f"https://discord.com/channels/{GUILD_ID}/{thread_id}"


def _add_escalate_content(embed: discord.Embed, node: dict, node_id: str):
    bug = _bug_by_node.get(node_id)
    if bug:
        tid = bug["discord_thread"]
        name = bug["name"]
        embed.description = (
            (embed.description or "").rstrip()
            + f"\n\nThis is a **known issue**: [{name}]({_thread_url(tid)})"
            + "\nPost a message there to get notified when it's resolved and help the developers with additional info."
        )
    else:
        embed.description = (
            (embed.description or "").rstrip()
            + f"\n\nHead to [#more-help]({MORE_HELP_URL}) and create a new post with the **Bug** tag."
        )
    collect = node.get("collect", [])
    if collect:
        prompts = "\n".join(f"- **{c['key']}**: {c['prompt']}" for c in collect)
        embed.add_field(name="Include in your post", value=prompts[:1024], inline=False)


def _render_solved() -> tuple[discord.Embed, discord.ui.View]:
    embed = discord.Embed(
        title="Issue resolved",
        description="Glad that's sorted! Use `/troubleshoot` again if anything else comes up.",
        colour=CLR_DONE,
    )
    return embed, discord.ui.View()



def _error_embed(msg: str) -> discord.Embed:
    return discord.Embed(title="Error", description=msg, colour=discord.Colour.red())


class TroubleshootSelect(discord.ui.Select):
    def __init__(self, node_id: str, options: list[dict]):
        select_opts = [
            discord.SelectOption(label=opt["label"][:100], value=opt["next"])
            for opt in options
        ]
        super().__init__(
            placeholder="Select your issue...",
            options=select_opts,
            custom_id=f"ts_sel:{node_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        _hit(self.values[0])
        embed, view = render_node(self.values[0])
        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except discord.InteractionResponded:
            pass


class TroubleshootButton(discord.ui.DynamicItem[discord.ui.Button],
                         template=r"ts:(?P<payload>.+)"):
    def __init__(self, payload: str) -> None:
        super().__init__(discord.ui.Button(label="...", custom_id=f"ts:{payload}"))
        self.payload = payload

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match.group("payload"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    async def callback(self, interaction: discord.Interaction):
        payload = self.payload

        if payload.endswith(":solved"):
            _hit(payload.removesuffix(":solved") + ":solved")
            embed, view = _render_solved()
        else:
            node_id = payload
            if node_id not in _nodes:
                base = node_id.rsplit(":", 1)[0]
                if base in _nodes:
                    node_id = base
            _hit(node_id)
            embed, view = render_node(node_id)

        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except discord.InteractionResponded:
            pass


class HelpChannelStart(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"ts_help_start"):
    def __init__(self) -> None:
        super().__init__(discord.ui.Button(
            label="Start Troubleshooting",
            custom_id="ts_help_start",
            style=discord.ButtonStyle.primary,
            emoji="\U0001f50d",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    async def callback(self, interaction: discord.Interaction):
        _hit("root")
        embed, view = render_node("root")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


def setup(client: discord.Client, cmd_tree: app_commands.CommandTree):
    """Register commands and wire up dynamic items."""
    client.add_dynamic_items(TroubleshootButton, HelpChannelStart)

    @cmd_tree.command(name="troubleshoot", description="Interactive troubleshooting guide")
    async def cmd_troubleshoot(interaction: discord.Interaction):
        _hit("root")
        embed, view = render_node("root")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @cmd_tree.command(name="troubleshoot-stats", description="Node hit counts")
    async def cmd_stats(interaction: discord.Interaction):
        rows = top_hits(25)
        if not rows:
            await interaction.response.send_message("No data yet.", ephemeral=True)
            return
        lines = [f"`{nid:<30}` {hits}" for nid, hits in rows]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @cmd_tree.command(name="init-help-channel",
                      description="Post the troubleshooting entry point in this channel")
    @app_commands.default_permissions(manage_channels=True)
    async def cmd_init_help(interaction: discord.Interaction):
        await interaction.response.send_message("Done.", ephemeral=True)
        embed = discord.Embed(
            title="Need help?",
            description=(
                "Before posting, try the troubleshooting guide below. "
                "It covers the most common issues and their fixes."
            ),
            colour=CLR_QUESTION,
        )
        view = discord.ui.View(timeout=None)
        view.add_item(HelpChannelStart())
        await interaction.channel.send(embed=embed, view=view)
