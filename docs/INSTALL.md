# Installing EDA Agent (guide for an AI agent)

You are an AI agent (Claude Code or another MCP client with a shell tool). A
user cloned this repository and asked you to install EDA Agent. Follow this
file step by step. For each step, either run the command yourself or give the
user the exact command to run, following the rules below.

The user's whole part is: clone the repository, open Claude Code in it and ask
you to install EDA Agent. Everything else is yours to guide. The
[README](../README.md) says what the tool does; this file is the procedure.

## Rules

1. **Ask before changing the machine.** Installing packages, setting
   environment variables, registering the MCP server and switching branches
   each change the user's system. Say what you are about to do, then run it or
   hand it over. Do not hide several of them behind one approval.
2. **Run it or hand it over, per step.** Run a command yourself when your shell
   tool works and the user agreed. Hand the user the exact command in a code
   block when your tool is blocked, when the step needs a secret, or when the
   user would rather type it. After a handed-over command, ask for the output
   (never a secret) and compare it with the "Expect" line.
3. **Never ask for a credential in the chat, and never type one yourself.** The
   user enters secrets in their own PowerShell window at a hidden prompt
   (step 7). You check only that a variable is set, never its value.
4. **No secrets or proprietary data in the repository.** Not in `config.toml`,
   `.mcp.json`, a `.env` file, a commit or a command that lands in a log. The
   repository is public. Exports, purchasing preferences and cart journals
   live in the export folder (step 6), outside it. The full list of what must
   never be committed, including project names, paths, designators and counts,
   is in the README section
   [Privacy: never commit real design data](../README.md#privacy-never-commit-real-design-data).
   When you show the user examples or report results, use synthetic names too.
5. **PowerShell on Windows.** Every command below is PowerShell. If a Bash tool
   is unavailable (it can fail to start on managed machines), use PowerShell.
6. **Check each step before the next.** Every step ends with a check. When one
   fails, stop and diagnose; do not build on a broken step.
7. **Report faithfully.** Say what was skipped and what failed.

Altium Designer is the user's own licensed install. Nothing here modifies it
other than registering a script project (step 4).

## Step 0. Find out the situation and ask the user

Work out which case you are in. Run:

```powershell
git rev-parse --show-toplevel
git branch --show-current
git status --short
git remote get-url origin
```

**Case A, an existing checkout (the usual one).** The first command prints a
folder whose `pyproject.toml` says `name = "eda-agent"`. That folder is
`<CLONE>`. **Do not clone again.** Use it as it is.

**Case B, nothing checked out yet.** Ask where the code should live (a short
path without spaces, such as `C:\Projects\eda-agent`) and clone it in step 2.

Then ask the user, and keep the answers:

1. **Which EDA tool?** This guide covers Altium Designer, Windows only. For
   KiCad or EasyEDA Pro, stop and follow [BACKENDS.md](BACKENDS.md).
2. **Where should proprietary exports go?** An absolute folder outside
   `<CLONE>` (called `<EXPORT_DIR>` below), for example
   `C:\Projects\eda-agent-export`. With no preference, the default is
   `EDA Agent\exports` in the home folder and nothing needs setting (step 6).
3. **Which distributors will they buy from?** DigiKey, Mouser, both or neither.
   Neither is fine: the install works and the purchasing tools report the
   distributor as unavailable.
4. **Which Claude Code scope?** `user` (every project, recommended) or `local`
   (this folder only).

### Which branch

Ordinary users run **`main`**. Use it unless you are told otherwise.

- Fresh clone: the default branch is `main`; stay on it.
- Existing checkout: if `git branch --show-current` is not `main`, ask whether
  that is deliberate (someone testing a feature branch) before changing it.
- **Never switch branches or pull while `git status --short` lists changes.**
  Ask the user what to do with them first.
- If the user wants the purchasing tools, confirm the checkout has them:

  ```powershell
  Test-Path <CLONE>\src\eda_agent\tools\purchasing.py
  ```

  `True` means you are fine. `False` means this checkout predates them. Run
  `git fetch origin`, then `git cat-file -e origin/main:src/eda_agent/tools/purchasing.py`.
  If that succeeds, `git switch main` and `git pull --ff-only` give the user
  the tools. If it fails, the purchasing tools are not on `main` yet and live
  on the branch `feature/distributor-pricing` (check with
  `git ls-remote --heads origin feature/distributor-pricing`); tell the user
  and ask before running `git switch feature/distributor-pricing`.

## Step 1. Prerequisites

```powershell
git --version
claude --version
py -0p
python --version
```

Expect: any recent Git and Claude Code, and at least one Python from **3.11 to
3.13** in the `py -0p` list (3.13 is fine and the test suite passes on it).
Newer versions may work when wheels exist for every dependency, but are not
tested.

**`python` and `py` can differ.** `python` is whatever comes first on `PATH`
and may be an older or a Microsoft Store build, while `py -0p` lists them all
and marks the default with `*`. Pick the interpreter on purpose, as `<PY>`,
for example `py -3.13` or `py -3.12`, and use it for the venv in step 2.

If `py` is not recognised, use `python` and its version as `<PY>`. If no
interpreter is 3.11 or newer, have the user install Python from python.org
(tick "Add python.exe to PATH") and open a new terminal.

Check that Altium Designer is installed, and note the version (AD20 or newer is
preferred):

```powershell
Get-ChildItem "C:\Program Files\Altium" -ErrorAction SilentlyContinue | Select-Object Name
```

Expect a folder such as `AD25`. If Altium is elsewhere, ask.

## Step 2. Code, virtual environment and editable install

**Case B only**, clone (the repository is public):

```powershell
git clone https://github.com/thomas92109/eda-agent.git <CLONE>
```

Then, in both cases:

```powershell
cd <CLONE>
Test-Path env\Scripts\python.exe
```

- **`False`:** create the environment (`env` and `.venv` are both ignored by
  Git):

  ```powershell
  <PY> -m venv env
  ```

- **`True`:** an environment already exists. Reuse it, after checking its
  version with `.\env\Scripts\python.exe --version`. If it is older than 3.11,
  ask, and recreate it with `<PY> -m venv env --clear`.

Install the package, **editable**, from the checkout:

```powershell
.\env\Scripts\python.exe -m pip install --upgrade pip
.\env\Scripts\python.exe -m pip install -e .
```

Why editable from the checkout and not `pip install eda-agent`: the checkout is
the source of truth (`git pull` updates it) and PyPI may carry an older release
without every tool. Optional extras, only on request: `.[render]` (PNG
previews), `.[route]` (faster router), `.[render3d]` (3D renders).

Check:

```powershell
.\env\Scripts\python.exe -c "import eda_agent; print(eda_agent.__file__)"
.\env\Scripts\python.exe -m eda_agent --version
```

Expect a path inside `<CLONE>\src\eda_agent\` and a version number. Anything
else: read the pip output and fix it first.

**Remember `<CLONE>\env\Scripts\python.exe`.** Step 5 registers exactly that
path. Never register a bare `python`: the one on `PATH` usually lacks this
package, the server exits at once, and Claude Code reports `CONNECTION_CLOSED`.

## Step 3. Offline health check

```powershell
.\env\Scripts\python.exe -m eda_agent health
```

This checks the workspace folder, the pointer file and the bundled scripts, and
needs neither Altium nor the internet. Expect `0 critical fail`. A warning
about the EasyEDA extension is irrelevant when the user works in Altium. Read
any other failure or warning it reports and fix it before going on. (On a
first install, the script lines may say nothing is registered yet; step 4
fixes that.)

## Step 4. Install and register the Altium scripts

```powershell
.\env\Scripts\python.exe -m eda_agent install-scripts
```

By default this copies the script project to wherever Altium already loads it
from, or to `%USERPROFILE%\EDA Agent\scripts\` the first time. `--dest PATH`
chooses another folder; `--force` overwrites existing scripts without asking.
Note the folder it reports: the file you need is `Altium_API.PrjScr` in it.

Registering it is a one-time click-through the user does in Altium (there is no
command for it):

1. **Preferences** (gear icon, top right) → **Scripting System** → **Global
   Projects** → **Install from file**.
2. Select the `Altium_API.PrjScr` that `install-scripts` reported.

Check: after restarting Altium, **File → Run Script...** lists `Altium_API`.

## Step 5. Register the MCP server with Claude Code

Use the full interpreter path from step 2. The `--` is required: it ends
`claude mcp add`'s own options. `--headless` leaves out the browser dashboard
window; drop it if the user wants the dashboard.

```powershell
claude mcp add altium -s user -- <CLONE>\env\Scripts\python.exe -m eda_agent --headless
```

Use `-s local` for a single-folder scope. If an `altium` entry already exists,
show it with `claude mcp get altium`, ask, and only then replace it with
`claude mcp remove altium -s user`.

Check:

```powershell
claude mcp list
```

Expect `altium: ... √ Connected`. If it fails, run the server by hand to see
the real error; a healthy one prints `Starting EDA Agent MCP Server` and waits
(Ctrl+C stops it):

```powershell
<CLONE>\env\Scripts\python.exe -m eda_agent --headless
```

## Step 6. Private export folder and `config.toml`

Everything proprietary the tools write goes to the export folder: purchasing
BOMs (in its `bom` subfolder), close-match reports, `config.toml` and the cart
journals. The server refuses any export path inside the repository.

**Default (nothing to set).** Without `EDA_AGENT_EXPORT_DIR`, exports go to
`EDA Agent\exports` under the user's home folder. Choose this if the user is
happy with it, and skip to "Create `config.toml`".

**Own folder.** `<EXPORT_DIR>` must be absolute and outside `<CLONE>`. Create
it and set the variable for the user account (it persists; no administrator
rights needed). A folder path is not a secret, so this is safe to run for them:

```powershell
New-Item -ItemType Directory -Force "<EXPORT_DIR>" | Out-Null
[Environment]::SetEnvironmentVariable('EDA_AGENT_EXPORT_DIR', '<EXPORT_DIR>', 'User')
```

A program only sees a changed user variable when it **starts afterwards**.
Ask the user to close and reopen Claude Code (and VS Code, if Claude Code runs
inside it). To also pin the folder on the server itself, register it there
(replace the entry from step 5):

```powershell
claude mcp remove altium -s user
claude mcp add altium -s user -e EDA_AGENT_EXPORT_DIR="<EXPORT_DIR>" -- <CLONE>\env\Scripts\python.exe -m eda_agent --headless
```

Check:

```powershell
[Environment]::GetEnvironmentVariable('EDA_AGENT_EXPORT_DIR','User')
claude mcp get altium
```

Expect the folder, and `EDA_AGENT_EXPORT_DIR=<EXPORT_DIR>` under `Environment:`
if you pinned it.

### Create `config.toml`

Purchasing preferences live in `<EXPORT_DIR>\config.toml` (or the default
folder). The first purchasing call creates it from a commented template and the
tool never rewrites it afterwards. To create it now:

```powershell
$env:EDA_AGENT_EXPORT_DIR = '<EXPORT_DIR>'   # leave this line out for the default folder
.\env\Scripts\python.exe -I -c "import sys; sys.path.insert(0, r'<CLONE>\src'); from eda_agent.export.purchasing_config import ensure_config; print(ensure_config())"
```

Expect `(WindowsPath('.../config.toml'), True, [])`. A `False` second value
means the file already existed and was left alone. Then go through the
settings that matter with the user and edit the file together:

| Key | Default | Meaning |
|---|---|---|
| `[assemblies] count` | 2 | Boards to build; a tool argument overrides it per call |
| `[overage] factor`, `max_unit_price` | 1.10, 5.00 | Extra parts, for inexpensive parts only |
| `[upgrade] extra_spend_percent`, `extra_spend_flat`, `price_break_window` | 10, 5.00, 2 | When a higher price break is worth taking |
| `[distributors] prefer`, `enabled` | digikey; both | Which distributor is tried first (`lowest` picks the cheapest) |
| `[review] stale_pricing_hours` | 4 | Age at which cached pricing is flagged |
| `[matching] auto_approve_verified_aliases` | false | Accept manufacturer-documented part number aliases without asking |

The full reference is in [PART_SOURCING.md](PART_SOURCING.md#settings-configtoml).
A key that looks like a credential is refused, so never put one here.

## Step 7. Distributor credentials (optional)

Skip this step if the user chose no distributors; everything else works without
it. Credentials live **only in environment variables**.

| Distributor | What the user needs | Variables | Needed for |
|---|---|---|---|
| DigiKey | An app in DigiKey's developer portal (an OAuth client) | `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET` | Pricing, stock, lifecycle |
| Mouser | A Search API key from their Mouser account | `MOUSER_API_KEY` | Pricing, stock, lifecycle |
| Mouser cart (only if carts are wanted) | A Cart and Order API key, a different key from the Search key | `MOUSER_CART_API_KEY` | `part_cart_*`, cart preparation |
| DigiKey saved lists (only if wanted) | The same app, a MyLists subscription and a registered redirect address | `DIGIKEY_REDIRECT_URI` if not `https://localhost`; `DIGIKEY_ACCOUNT_ID` for several accounts | `part_list_*` |

Help the user obtain each one on the distributor's own site. Then the **user**
enters them, in their **own** PowerShell window (not through you). Give them
this block as it is. `Read-Host -AsSecureString` hides what they type, so the
secret appears neither on screen nor in the shell history nor in this chat:

```powershell
function Set-UserSecret([string]$Name) {
    $secure = Read-Host "Enter $Name (input is hidden)" -AsSecureString
    $plain = [System.Net.NetworkCredential]::new('', $secure).Password
    if (-not $plain) { "$Name left unchanged (nothing entered)"; return }
    [Environment]::SetEnvironmentVariable($Name, $plain, 'User')
    "$Name saved ($($plain.Length) characters)"
}
Set-UserSecret DIGIKEY_CLIENT_ID
Set-UserSecret DIGIKEY_CLIENT_SECRET
Set-UserSecret MOUSER_API_KEY
# only if carts are wanted:
# Set-UserSecret MOUSER_CART_API_KEY
```

Tell the user plainly how this is stored: a user environment variable is kept
in their Windows profile, readable by programs running as them, and is **not
encrypted**. It is as private as the account, and nothing goes into the
repository. Then they **fully close and reopen Claude Code** so the server
inherits the variables.

Do not use these instead:

- `claude mcp add -e NAME=value` writes the secret in plain text into
  `~/.claude.json`.
- `setx NAME value` puts the secret in the command line and the shell history.
- A `.env` file in the clone, even though Git ignores it.
- Pasting a key into the chat or into a config file you edit.

### Check without revealing anything

Print only whether each variable is set, and how long it is, never its value:

```powershell
foreach ($n in 'DIGIKEY_CLIENT_ID','DIGIKEY_CLIENT_SECRET','MOUSER_API_KEY','MOUSER_CART_API_KEY') {
  $v = [Environment]::GetEnvironmentVariable($n,'User')
  "{0}: {1}" -f $n, $(if ($v) { "set ($($v.Length) chars)" } else { "not set" })
}
```

Expect `set (...)` for every variable the user chose. Whether the distributor
accepts the credentials is checked in step 9.

## Step 8. Start the Altium side

The user opens their project in Altium, then starts the polling loop:

1. **File → Run Script...**
2. Expand `Altium_API` → `Dispatcher.pas`, select **StartMCPServer**, click
   **Run**.

A status window appears. Leave it open: it is the live connection (its
**Detach** button ends the loop cleanly). As an alternative to the menu,
`<CLONE>\env\Scripts\python.exe -m eda_agent relaunch` asks the running Altium
to start the loop; if it does not work, use the menu.

Then the Claude Code session must start (or restart) so it loads the server.
The conversation can continue: ask the user to quit and run `claude --continue`
in `<CLONE>`, or to reconnect with `/mcp`.

## Step 9. Verify the connection (read-only)

Nothing below changes a design or writes a file.

1. `claude mcp list` shows `altium` as Connected.
2. Call `app_get_status`: expect `running: true`. Then `app_attach`: expect
   `attached: true`. If it stays false, the loop from step 8 is not running.
3. Call `proj_get_focused`: expect the project the user has open. Confirm it
   with them.
4. From a terminal, for a full preflight (process, loop, version and a
   read-only test command): `.\env\Scripts\python.exe -m eda_agent doctor`
   (`--json` for machine-readable output).
5. **Distributors** (only if configured): call `part_search` with
   `query="LM358DR"` (a public part number, so nothing of the user's is sent),
   `limit_per_provider=1`. Each provider answers on its own: a DigiKey or
   Mouser result with a `sourcing` block means its credentials work; a provider
   that cannot answer says why and names the variable it wants.

Report the results to the user, including anything that is not green.

### Optional: a first purchasing export (this writes a file)

Ask first, because this one writes a CSV into the export folder (and contacts
the distributors with the project's part numbers). With the project focused,
call `part_purchasing_export_csv` with `assemblies` set to the user's number.
Expect `ok: true`, an `output_path` inside the export folder's `bom` subfolder,
row counts and, per configured distributor, `asked` / `matched` counts with
`ok: true`. `blocked` and `review` rows are normal on a first run and are
explained in the CSV's Review Reasons column; nothing from a blocked row may be
ordered. Run `git status` in `<CLONE>`: it must show no export. Excel mangles
some values when it opens a CSV directly (leading zeros, long numbers); import
through **Data → From Text/CSV** instead.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Failed to connect` / `CONNECTION_CLOSED` | The registered command is a bare `python` without the package | Re-register with the full path to `<CLONE>\env\Scripts\python.exe` (step 5) |
| `No module named eda_agent` | Wrong interpreter, or the editable install failed | Use the venv's `python.exe`; rerun `pip install -e .` in `<CLONE>` |
| `python` is not the version you chose | `PATH` order differs from the `py` launcher | Create the venv with `<PY> -m venv env` (step 2) and use only the venv's interpreter |
| `'eda-agent' is not recognized` | The console script is not on `PATH` | Use `<CLONE>\env\Scripts\python.exe -m eda_agent ...` |
| Bash tool: `EPERM ... uv_spawn ... bash.exe` | The shell tool cannot start | Use PowerShell; hand over steps that need the user's own terminal |
| `is inside the public repository` | `EDA_AGENT_EXPORT_DIR` or an output path points into the clone | Use a folder outside `<CLONE>` (step 6) |
| `EDA_AGENT_EXPORT_DIR must be an absolute path` | The variable holds a relative path | Set a full path such as `C:\...` |
| Exports go to the home folder, not `<EXPORT_DIR>` | The variable was set after Claude Code started, or is missing for the server | Restart Claude Code; check `claude mcp get altium`; or pin it with `-e` (step 6) |
| A provider is unavailable and names a variable | That credential is not in the server's environment | Set it (step 7) and fully restart Claude Code |
| A distributor answers but rejects the credential | Wrong or unapproved key (a DigiKey app without the API enabled; the Mouser Search key used for carts) | Recheck the key in the distributor's portal and re-enter it with `Set-UserSecret` |
| `attached: false`, or Altium tools time out | The loop is not running | Start `StartMCPServer` (step 8) and keep its window open |
| `ambiguous: true` in `app_get_status` | More than one Altium is running | Close the extras and retry |
| `Altium_API` is missing from **Run Script** | The script project is not registered | Redo the registration in step 4, then restart Altium |
| The server reports updates you did not expect | The background update check looks at the upstream project, not at your fork | Ignore it, or set `EDA_AGENT_UPDATE_CHECK=0` for the server |

## Updating

Only for a clean checkout on `main`:

```powershell
cd <CLONE>
git status --short          # must print nothing
git pull --ff-only
.\env\Scripts\python.exe -m pip install -e .
.\env\Scripts\python.exe -m eda_agent install-scripts
.\env\Scripts\python.exe -m eda_agent relaunch --restart
```

Then reconnect the server (`/mcp`). `eda-agent update` (add `--check` to only
look) does the same after asking, and refuses on a checkout that is not on
`main`, has local changes or has commits of its own. `config.toml` is never
rewritten, so keys added by a newer release, such as
`[matching] auto_approve_verified_aliases`, are added by hand.

## Uninstalling

Ask before each step. The first two undo themselves; the last cannot be undone.

```powershell
claude mcp remove altium -s user
[Environment]::SetEnvironmentVariable('EDA_AGENT_EXPORT_DIR', $null, 'User')
```

Remove any distributor variables the same way, one per name. Unregister
`Altium_API.PrjScr` in Altium under **Scripting System → Global Projects**.
Delete `<CLONE>` only when the user asks. **Never delete the export folder
without explicit confirmation**: it holds their BOMs, purchasing preferences
and cart journals.
