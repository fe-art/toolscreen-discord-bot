# Toolscreen Discord Bot

Support bot for the [Toolscreen](https://github.com/jojoe77777/Toolscreen) project's Discord server. It deflects common support requests through a self-service `/troubleshoot` command so that **#more-help** stays focused on genuinely new or unresolved bugs. Users whose issue isn't covered are guided to open (or join) a bug report there.

## Troubleshooting command

`/troubleshoot` walks the user through an interactive decision tree loaded from `troubleshooting-tree.yaml`. The tree was built from recurring issues in **#help** and **#more-help**, structured as a YAML graph of nodes that point to each other:

```
checked A? -> yes: go to check B
           -> no:  show solution A
```

If none of the known fixes apply, the user is prompted to create a post in **#more-help** with the relevant diagnostic info pre-listed.

Every node visit and solve is counted in `bot.db` (`node_hits` table). A solve records the node that fixed it (e.g. `sol_crash_f11:solved`), so `/troubleshoot-stats` shows which paths users take most and which fixes actually work. Use this to prune dead branches and prioritize common issues.

The knowledge base is only as good as the data behind it. Contributions to `troubleshooting-tree.yaml` are welcome to keep it up to date.

## Bug triage listener

The bot watches forum channels listed in `watched_channel_ids`. When a new post is created with the **Bug** tag, it replies with a structured triage template asking for OS, Toolscreen version, Minecraft version, launcher, logs, etc. Threads with no activity after a configurable period are automatically tagged as done and archived.

## Setup

```
python -m venv .venv
.venv/bin/pip install .
cp config.example.yaml config.yaml   # fill in your values
.venv/bin/python bot.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.
