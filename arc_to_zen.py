#!/usr/bin/env python3
"""Move your Arc sidebar (Spaces, pinned tabs, folders, Favorites) into Zen.

    python3 arc_to_zen.py --dry-run     # show what would be imported, change nothing
    python3 arc_to_zen.py               # import (quit Zen first)
    python3 arc_to_zen.py --undo        # put Zen back the way it was before the last import

What maps to what:
    Arc Space              -> Zen Space with the same name (created if missing)
    Arc pinned tabs        -> Zen pinned tabs, Arc folders -> Zen folders (nesting kept)
    Arc Favorites          -> Zen Essentials
    Arc Today (unpinned)   -> Zen unpinned tabs (Zen only keeps these if it restores
                              the previous session on startup)

Running it again replaces tabs from earlier runs instead of duplicating them.
Only the Python 3 standard library is used. Tested on macOS with Zen's
session format as of October 2026.
"""
import argparse
import configparser
import copy
import json
import os
import platform
import shutil
import struct
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

MOZLZ4_MAGIC = b"mozLz40\0"
SESSION_FILE = "zen-sessions.jsonlz4"
BACKUP_ROOT = Path(os.environ.get("ARC_TO_ZEN_BACKUPS", Path.home() / "arc-to-zen-backups"))
SYSTEM = platform.system()


class Abort(Exception):
    pass


# --- mozLz4 (Mozilla's LZ4 block format) -------------------------------------

def lz4_block_decompress(src, size):
    dst = bytearray()
    i = 0
    while i < len(src):
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= len(src):
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        match = (token & 0xF) + 4
        if (token & 0xF) == 15:
            while True:
                b = src[i]
                i += 1
                match += b
                if b != 255:
                    break
        start = len(dst) - offset
        for k in range(match):
            dst.append(dst[start + k])
    if len(dst) != size:
        raise Abort(f"Corrupt session file: expected {size} bytes, got {len(dst)}")
    return bytes(dst)


def lz4_block_literals_only(data):
    """A valid LZ4 block holding one uncompressed literal run."""
    n = len(data)
    out = bytearray()
    if n < 15:
        out.append(n << 4)
    else:
        out.append(0xF0)
        rest = n - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    out += data
    return bytes(out)


def read_mozlz4(path):
    raw = Path(path).read_bytes()
    if raw[:8] != MOZLZ4_MAGIC:
        raise Abort(f"{path} is not a Mozilla lz4 file")
    (size,) = struct.unpack("<I", raw[8:12])
    return json.loads(lz4_block_decompress(raw[12:], size))


def write_mozlz4(path, obj):
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    tmp = Path(str(path) + ".arc-to-zen.tmp")
    tmp.write_bytes(MOZLZ4_MAGIC + struct.pack("<I", len(payload)) + lz4_block_literals_only(payload))
    if read_mozlz4(tmp) != obj:
        tmp.unlink()
        raise Abort("Round-trip check of the new session file failed; nothing was changed.")
    os.replace(tmp, path)


# --- Locating Arc and Zen ----------------------------------------------------

def find_arc_sidebar():
    home = Path.home()
    candidates = []
    if SYSTEM == "Darwin":
        candidates.append(home / "Library/Application Support/Arc/StorableSidebar.json")
    elif SYSTEM == "Windows":
        packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
        candidates += sorted(packages.glob("TheBrowserCompany.Arc*/LocalCache/Local/Arc/StorableSidebar.json"))
    for path in candidates:
        if path.is_file():
            return path
    raise Abort("Couldn't find Arc's StorableSidebar.json. Pass it with --arc PATH.")


def zen_roots():
    home = Path.home()
    if SYSTEM == "Darwin":
        return [home / "Library/Application Support/zen"]
    if SYSTEM == "Windows":
        return [Path(os.environ.get("APPDATA", "")) / "zen"]
    return [home / ".zen", home / ".var/app/app.zen_browser.zen/.zen"]


def find_zen_profile():
    for root in zen_roots():
        ini = root / "profiles.ini"
        if not ini.is_file():
            continue
        cfg = configparser.ConfigParser()
        cfg.read(ini)

        def resolve(section):
            path = cfg[section]["Path"]
            return root / path if cfg[section].get("IsRelative", "1") == "1" else Path(path)

        # An [Install...] section names the profile the installed Zen actually uses.
        for section in cfg.sections():
            if section.startswith("Install") and "Default" in cfg[section]:
                return root / cfg[section]["Default"]
        profiles = [s for s in cfg.sections() if s.startswith("Profile")]
        for section in profiles:
            if cfg[section].get("Default") == "1":
                return resolve(section)
        if len(profiles) == 1:
            return resolve(profiles[0])
        names = ", ".join(cfg[s].get("Name", s) for s in profiles)
        raise Abort(f"Several Zen profiles found ({names}). Pass one with --profile PATH.")
    raise Abort("Couldn't find a Zen profile. Open Zen once, quit it, then run this again.")


def zen_is_using(profile):
    """True if a running Zen holds this profile's lock file."""
    if SYSTEM == "Windows":
        lock = profile / "parent.lock"
        if not lock.exists():
            return False
        try:
            # Zen opens parent.lock without sharing, so this fails while it runs.
            os.close(os.open(lock, os.O_RDWR))
            return False
        except PermissionError:
            return True

    import fcntl
    lock = profile / ".parentlock"
    if not lock.exists():
        return False
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.lockf(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


# --- Reading Arc --------------------------------------------------------------

class ArcSidebar:
    def __init__(self, data):
        try:
            container = next(c for c in data["sidebar"]["containers"] if "spaces" in c)
        except (KeyError, StopIteration):
            raise Abort("Arc's sidebar file isn't in a format this script understands.")
        self.items = {x["id"]: x for x in container["items"] if isinstance(x, dict)}
        self.spaces = [x for x in container["spaces"] if isinstance(x, dict)]
        self.top_apps_ids = [x for x in container.get("topAppsContainerIDs", []) if isinstance(x, str)]

    def tree(self, item_id):
        """Children of item_id: ("tab", title, url) or ("folder", title, [children])."""
        nodes = []
        for cid in self.items.get(item_id, {}).get("childrenIds", []):
            item = self.items.get(cid)
            if item is None:
                continue
            kind, payload = next(iter(item["data"].items()))
            if kind == "tab":
                url = payload.get("savedURL")
                if url:
                    nodes.append(("tab", item.get("title") or payload.get("savedTitle") or url, url))
            elif kind == "list":
                nodes.append(("folder", item.get("title") or "Untitled folder", self.tree(cid)))
            else:
                # Easels, split views, etc.: keep their tabs at this level.
                nodes += self.tree(cid)
        return nodes

    def favorites(self):
        nodes = []
        for cid in self.top_apps_ids:
            nodes += self.tree(cid)
        return nodes

    def space_trees(self, space):
        # containerIDs alternates label and id: ["pinned", <id>, "unpinned", <id>]
        ids = space.get("containerIDs", [])
        containers = dict(zip(ids[0::2], ids[1::2]))
        return self.tree(containers.get("pinned")), self.tree(containers.get("unpinned"))


# --- Building Zen session data ------------------------------------------------

class ZenBuilder:
    def __init__(self, now_ms, collapsed):
        self.now_ms = now_ms
        self.collapsed = collapsed
        self.counter = 0
        self.tabs = []
        self.folders = []
        self.groups = []

    def new_folder_id(self):
        self.counter += 1
        return f"{self.now_ms}-{self.counter}"

    def tab(self, title, url, ws, pinned, essential, group_id=None):
        tab = {
            "entries": [{"url": url, "title": title, "triggeringPrincipal_base64": '{"3":{}}'}],
            "index": 1,
            "lastAccessed": self.now_ms,
            "hidden": False,
            "attributes": {},
            "userContextId": 0,
            "zenWorkspace": ws,
            "zenSyncId": f"{self.now_ms}-{uuid.uuid4()}",
            "zenEssential": essential,
            "pinned": pinned,
            "zenDefaultUserContextId": "true",
            "zenPinnedIcon": None,
            "zenIsEmpty": False,
            "zenHasStaticIcon": False,
            "zenGlanceId": None,
            "zenIsGlance": False,
        }
        if group_id:
            tab["groupId"] = group_id
        return tab

    def add_flat(self, nodes, ws, pinned, essential):
        for node in nodes:
            if node[0] == "tab":
                self.tabs.append(self.tab(node[1], node[2], ws, pinned, essential))
            else:
                self.add_flat(node[2], ws, pinned, essential)

    def add_pinned(self, nodes, ws, folders):
        for node in nodes:
            if node[0] == "tab":
                self.tabs.append(self.tab(node[1], node[2], ws, True, False))
            elif folders:
                self.add_folder(node[1], node[2], ws, self.new_folder_id(), parent_id=None, prev=None)
            else:
                self.add_flat(node[2], ws, True, False)

    def add_folder(self, name, nodes, ws, fid, parent_id, prev):
        """One folder: placeholder tab, its direct tabs, then its subfolders.

        Each folder's tabs stay contiguous in the tab list. Zen moves nested
        folders into place on restore using parentId and prevSiblingInfo.
        """
        empty = self.tab("", "about:blank", ws, True, False, group_id=fid)
        empty["zenIsEmpty"] = True
        empty["zenDefaultUserContextId"] = None
        self.tabs.append(empty)
        self.groups.append({
            "pinned": True, "splitView": False, "id": fid, "name": name,
            "color": "zen-workspace-color", "collapsed": self.collapsed, "saveOnWindowClose": True,
        })
        self.folders.append({
            "pinned": True, "splitViewGroup": False, "id": fid, "name": name,
            "collapsed": self.collapsed, "saveOnWindowClose": True, "parentId": parent_id,
            "prevSiblingInfo": prev or {"type": "start", "id": None},
            "emptyTabIds": [empty["zenSyncId"]], "userIcon": "", "workspaceId": ws,
        })

        prev_sibling = {"type": "tab", "id": empty["zenSyncId"]}
        subfolders = []
        for node in nodes:
            if node[0] == "tab":
                tab = self.tab(node[1], node[2], ws, True, False, group_id=fid)
                self.tabs.append(tab)
                prev_sibling = {"type": "tab", "id": tab["zenSyncId"]}
            else:
                sub_id = self.new_folder_id()
                subfolders.append((node, sub_id, prev_sibling))
                prev_sibling = {"type": "group", "id": sub_id}
        for node, sub_id, prev in subfolders:
            self.add_folder(node[1], node[2], ws, sub_id, parent_id=fid, prev=prev)


def drop_previous_imports(zen, prefixes):
    if not prefixes:
        return 0
    starts = tuple(p + "-" for p in prefixes)
    before = sum(1 for t in zen["tabs"] if not t.get("zenIsEmpty"))
    zen["tabs"] = [t for t in zen["tabs"] if not t.get("zenSyncId", "").startswith(starts)]
    used = {t.get("groupId") for t in zen["tabs"]}
    zen["folders"] = [f for f in zen.get("folders") or [] if f["id"] in used]
    zen["groups"] = [g for g in zen.get("groups") or [] if g["id"] in used]
    return before - sum(1 for t in zen["tabs"] if not t.get("zenIsEmpty"))


# --- Backups ------------------------------------------------------------------

def backups_for(profile, include_restored=True):
    if not BACKUP_ROOT.is_dir():
        return []
    found = []
    for meta_path in sorted(BACKUP_ROOT.glob("*/meta.json")):
        meta = json.loads(meta_path.read_text())
        if meta.get("profile") != str(profile):
            continue
        if meta.get("restored") and not include_restored:
            continue
        found.append((meta_path.parent, meta))
    return found


def make_backup(profile, import_prefix):
    dest = BACKUP_ROOT / f"{datetime.now():%Y%m%d-%H%M%S}-{import_prefix}"
    dest.mkdir(parents=True)
    shutil.copy2(profile / SESSION_FILE, dest / SESSION_FILE)
    if (profile / "zen-sessions-backup").is_dir():
        shutil.copytree(profile / "zen-sessions-backup", dest / "zen-sessions-backup")
    (dest / "meta.json").write_text(json.dumps({"profile": str(profile), "import_prefix": import_prefix}))
    return dest


# --- Commands -----------------------------------------------------------------

def print_tree(nodes, indent):
    for node in nodes:
        if node[0] == "folder":
            print(f"{indent}[{node[1]}]")
            print_tree(node[2], indent + "  ")
        else:
            print(f"{indent}{node[1]}")


def count_tabs(nodes):
    return sum(1 if n[0] == "tab" else count_tabs(n[2]) for n in nodes)


def run_import(args, profile):
    arc_path = Path(args.arc) if args.arc else find_arc_sidebar()
    arc = ArcSidebar(json.loads(arc_path.read_text()))
    session_path = profile / SESSION_FILE
    if not session_path.is_file():
        raise Abort(f"{session_path} doesn't exist. Open Zen once, quit it, then run this again.")
    zen = read_mozlz4(session_path)
    if not isinstance(zen.get("spaces"), list) or not zen["spaces"] or "tabs" not in zen:
        raise Abort("Zen's session file isn't in a format this script understands (Zen may have changed it).")
    if not args.dry_run and zen_is_using(profile):
        raise Abort("Zen is running. Quit Zen completely (Cmd+Q / File > Quit), then run this again.")

    print(f"Arc:  {arc_path}")
    print(f"Zen:  {profile}\n")

    favorites = arc.favorites()
    print(f"Favorites -> Essentials: {count_tabs(favorites)} tabs")
    plan = []
    for space in arc.spaces:
        name = space.get("title") or "Untitled"
        pinned, unpinned = arc.space_trees(space)
        plan.append((name, pinned, unpinned))
        print(f"\nSpace {name}: {count_tabs(pinned)} pinned, {count_tabs(unpinned)} unpinned")
        if args.verbose:
            print_tree(pinned, "  ")

    if args.dry_run:
        print("\nDry run: nothing was changed.")
        return

    old_prefixes = [m["import_prefix"] for _, m in backups_for(profile) if m.get("import_prefix")]
    dropped = drop_previous_imports(zen, old_prefixes)

    zen_spaces = {s["name"]: s for s in zen["spaces"]}
    for name, _, _ in plan:
        if name not in zen_spaces:
            new_space = copy.deepcopy(zen["spaces"][0])
            new_space["uuid"] = "{%s}" % uuid.uuid4()
            new_space["name"] = name
            zen["spaces"].append(new_space)
            zen_spaces[name] = new_space
            print(f"Created Zen Space {name}")

    builder = ZenBuilder(int(time.time() * 1000), collapsed=not args.expanded)
    builder.add_flat(favorites, zen["spaces"][0]["uuid"], pinned=True, essential=True)
    for name, pinned, _ in plan:
        builder.add_pinned(pinned, zen_spaces[name]["uuid"], folders=not args.flatten)
    if not args.no_today:
        for name, _, unpinned in plan:
            builder.add_flat(unpinned, zen_spaces[name]["uuid"], pinned=False, essential=False)

    # Zen keeps Essentials first, then pinned tabs, then unpinned ones.
    existing = zen["tabs"]
    order = lambda t: (not t.get("zenEssential"), not t.get("pinned"))  # noqa: E731
    zen["tabs"] = (
        sorted([t for t in builder.tabs if t["pinned"]] + [t for t in existing if t.get("pinned")], key=order)
        + [t for t in builder.tabs if not t["pinned"]]
        + [t for t in existing if not t.get("pinned")]
    )
    zen["folders"] = (zen.get("folders") or []) + builder.folders
    zen["groups"] = (zen.get("groups") or []) + builder.groups
    zen["lastCollected"] = builder.now_ms

    backup = make_backup(profile, str(builder.now_ms))
    write_mozlz4(session_path, zen)

    imported = sum(1 for t in builder.tabs if not t["zenIsEmpty"])
    print(f"\nImported {imported} tabs and {len(builder.folders)} folders.")
    if dropped:
        print(f"Replaced {dropped} tabs from an earlier import.")
    print(f"Backup: {backup}")
    print("Start Zen to see it. To roll back: python3 arc_to_zen.py --undo")


def run_undo(profile):
    found = backups_for(profile, include_restored=False)
    if not found:
        raise Abort(f"No backups left to restore for {profile} in {BACKUP_ROOT}.")
    if zen_is_using(profile):
        raise Abort("Zen is running. Quit Zen completely (Cmd+Q / File > Quit), then run this again.")
    backup, meta = found[-1]
    shutil.copy2(backup / SESSION_FILE, profile / SESSION_FILE)
    # Mark it used so a second --undo goes one step further back.
    meta["restored"] = True
    (backup / "meta.json").write_text(json.dumps(meta))
    print(f"Restored Zen's session from {backup}")


def main():
    parser = argparse.ArgumentParser(description="Move your Arc sidebar into Zen.")
    parser.add_argument("--dry-run", action="store_true", help="show what would be imported, change nothing")
    parser.add_argument("--undo", action="store_true", help="restore Zen's session from before the last import")
    parser.add_argument("--verbose", "-v", action="store_true", help="list every tab in the dry run")
    parser.add_argument("--flatten", action="store_true", help="don't create folders, pin every tab at the top level")
    parser.add_argument("--expanded", action="store_true", help="import folders open instead of collapsed")
    parser.add_argument("--no-today", action="store_true", help="skip Arc's unpinned Today tabs")
    parser.add_argument("--arc", metavar="PATH", help="Arc's StorableSidebar.json (found automatically)")
    parser.add_argument("--profile", metavar="PATH", help="Zen profile folder (found automatically)")
    args = parser.parse_args()

    try:
        profile = Path(args.profile).expanduser() if args.profile else find_zen_profile()
        if args.undo:
            run_undo(profile)
        else:
            run_import(args, profile)
    except Abort as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
