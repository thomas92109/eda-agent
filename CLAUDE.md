# Instructions for Claude Code

This repository is public. Proprietary engineering and purchasing data must never be written inside it.

## Proprietary exports

- All proprietary BOMs, purchasing plans, CSV/Excel exports, and distributor reports belong in the folder named by the `EDA_AGENT_EXPORT_DIR` environment variable.
- Never write proprietary data inside the public repository.
- If you write such files directly, rather than through the MCP tools, resolve `EDA_AGENT_EXPORT_DIR` first. When it is unset, the default is `EDA Agent/exports` under the user's home folder. The rules are in `src/eda_agent/export/destination.py`.
- Purchasing preferences (`config.toml`) and the cart journals (`purchasing/journal`) also live in that folder, never in the repository. They hold quantities and part numbers, so they are as private as an export.
- Do not include proprietary export files in Git commits. Check `git status` before staging, and never use `git add -f` on an export.
