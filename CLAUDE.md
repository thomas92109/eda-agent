# Instructions for Claude Code

## Installing EDA Agent

When the user asks to install, set up or configure EDA Agent (or its MCP server, the Altium scripts, the export folder or the distributor credentials), read `docs/INSTALL.md` first and follow it step by step. Do not improvise the procedure from the README. The guide says which commands to run yourself and which to hand to the user. Never ask the user to paste an API key or secret into the chat.

## Proprietary data

This repository is public. Proprietary engineering and purchasing data must never be written inside it. The full rules are in the README section "Privacy: never commit real design data".

Never write any of these into a tracked file, a commit message, a comment or an example:

- real Altium or KiCad design files, BOMs, netlists, manufacturing exports, or purchasing data;
- credentials of any kind;
- identifying project metadata: project, document or library names, real design paths, designators, nets, and counts or sizes from a real design.

Use synthetic examples (such as `Example-Board`). EDA Agent's own paths and public manufacturer part numbers used as examples are fine. Describe a live validation generically, without the project's name or figures. If private data may already be staged or committed, stop and tell the user; do not push and do not rewrite history on your own.

## Proprietary exports

- All proprietary BOMs, purchasing plans, CSV/Excel exports, and distributor reports belong in the folder named by the `EDA_AGENT_EXPORT_DIR` environment variable.
- Never write proprietary data inside the public repository.
- If you write such files directly, rather than through the MCP tools, resolve `EDA_AGENT_EXPORT_DIR` first. When it is unset, the default is `EDA Agent/exports` under the user's home folder. The rules are in `src/eda_agent/export/destination.py`.
- Purchasing preferences (`config.toml`) and the cart journals (`purchasing/journal`) also live in that folder, never in the repository. They hold quantities and part numbers, so they are as private as an export.
- Do not include proprietary export files in Git commits. Check `git status` before staging, and never use `git add -f` on an export.
