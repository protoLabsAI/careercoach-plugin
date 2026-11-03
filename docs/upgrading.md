# Upgrading

Version notes that change behavior. Release notes for every version are on the
[releases page](https://github.com/protoLabsAI/careercoach-plugin/releases).

## 0.13.9

The coach's voice rules now ride on every turn, and the writing guide is its own `writing-style` skill. If a
prompt or skill of yours told the agent to read `apply-form/writing-style.md`, point it at
`load_skill("writing-style")` instead.

## 0.13.2

A blank **Role-packet workspace** (`packet_root`) now uses this agent's own folder inside its data directory
(`<agent data dir>/workspace`), so agents on one machine never share files.

If your agent relied on the old blank setting to read `~/CareerCoach`, set `packet_root: ~/CareerCoach` in
**Settings → Career Coach → Role-packet workspace** before upgrading to keep using that folder. Otherwise
your role packets and `Experience.md` stay where they are and aren't moved. The plugin logs a one-time
warning when it sees a populated `~/CareerCoach` it no longer reads.

## 0.8.0

A relative `packet_root` or `CAREERCOACH_PACKET_DIR` now resolves under your home directory, not the
server's working directory. This only matters for source or dev runs: on the desktop app the working
directory is `/`, which is read-only, so a relative workspace never worked there. An absolute path or `~/…`
behaves as before.

## 0.7

The profile and tracker moved from `~/.protoagent/careercoach/<instance>/` to this agent's plugin store,
`<instance_root>/careercoach/`. A store found in the old place is copied forward once, and the old folder
gets a `MIGRATED-TO` note. The old files stay for a rollback but are never adopted again, so wiping the new
store really does start over. If you restore a machine from a backup taken before 0.7, that note is already
there, so copy the files across by hand or delete the note.
