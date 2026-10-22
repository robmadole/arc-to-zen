# arc-to-zen

Moves your Arc sidebar into the Zen browser: Spaces, pinned tabs, folders (including nested ones) and Favorites.

| Arc | Zen |
|---|---|
| Space | Space with the same name (created if missing) |
| Pinned tabs | Pinned tabs |
| Folders | Folders, nesting and order kept, imported collapsed |
| Favorites | Essentials |
| Today (unpinned) tabs | Unpinned tabs* |

\* Zen only keeps unpinned tabs after a restart if Settings → General → Startup → "Open previous windows and tabs" is on.

Not moved: Boosts, Easels, split views, Little Arc, history, passwords. For history and passwords, try Zen's own **File → Import From Another Browser**.

## Use it

You need Python 3. Check with `python3 --version`. On a Mac without it, run `xcode-select --install`.

1. Open Zen at least once so it has a profile, then **quit Zen** (Cmd+Q). The script won't run while Zen is open.
2. Preview what will be imported. This changes nothing:
   ```
   python3 arc_to_zen.py --dry-run -v
   ```
3. Import:
   ```
   python3 arc_to_zen.py
   ```
4. Start Zen.

Arc can stay open the whole time. The script only reads Arc's data.

## Undo

Quit Zen, then:

```
python3 arc_to_zen.py --undo
```

Each run backs up Zen's session file to `~/arc-to-zen-backups/` first. `--undo` restores the most recent backup. Run `--undo` again to go back another step.

## Running it again

Running it a second time replaces the tabs from the first run instead of adding duplicates. Tabs and folders you made in Zen yourself are left alone.

## Options

| Flag | What it does |
|---|---|
| `--dry-run` | Show what would be imported, change nothing |
| `-v` | With `--dry-run`, list every tab and folder |
| `--flatten` | No folders: every tab pinned at the top level of its Space |
| `--expanded` | Import folders open instead of collapsed |
| `--no-today` | Skip Arc's unpinned Today tabs |
| `--undo` | Restore Zen from before the last import |
| `--arc PATH` | Use this `StorableSidebar.json` instead of finding it |
| `--profile PATH` | Use this Zen profile folder instead of the default one |

If you have several Zen profiles and none is the default, the script lists them and asks for `--profile`. Find your profile folders in Zen at `about:profiles`.

## How it works

Arc keeps its sidebar in `~/Library/Application Support/Arc/StorableSidebar.json`. Zen keeps Spaces, pinned tabs and folders in `zen-sessions.jsonlz4` in its profile folder. The script reads the first file and writes the second. It uses only the Python standard library.

Tested on macOS with Zen as of October 2026. The Windows and Linux paths are written in but haven't been tried. If Zen changes its session format, the script stops with an error instead of writing a file it doesn't understand. If Zen ever comes up wrong after an import, quit it and run `--undo`.
