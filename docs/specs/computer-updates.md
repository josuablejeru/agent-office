# Rebuild the system image and update agents' computers

> **Status (26 Nov):** built. Checked on real VMs (`tests/e2e/test_update.py`, scratch data
> directory): an agent with a file in its Shared folder, a 5 MB random file, a database in WAL
> mode and a memory entry was moved onto a new image version; all four arrived intact
> (checksum compared), a file outside the carried places did not, the disk now backs onto the
> new image, nothing was left behind, and stopping the computer during the update was refused.
> The rest of the e2e suite still passes. Unit tests cover packing and unpacking, refusing
> archives that reach outside the carried places, image versions and their removal, a rebuild
> beside an image in use, an update that fails while unpacking (the old disk is put back), and
> one cut short by quitting the app (undone at the next start).
>
> Not checked: a real rebuild through the app (the e2e test uses an APFS clone as the "new"
> image, not a 20-minute build); the Settings › Computers page in the running app; carrying
> more than a few MB; an old computer that cannot boot any more.

**For:** you, after changing the image's sources (a company Debian mirror, a hardened image,
company certificates), or to pick up Debian's updates, with agents that already have work on
their computers.
**Outcome:** Settings › Computers › Rebuild system image, then Update (or Update all); each
agent ends up on the new image with its Shared folder, databases and memory.

## How it works
- **Versions, never replaced in place.** Agent disks are qcow2 layers naming their image's full
  path, so a rebuild writes `images/debian-desktop-<UTC time>.qcow2` beside the one in use and,
  only after it succeeded, points `images/debian-desktop.current` at it. New computers start
  from the current version; existing ones keep theirs. A version no disk uses (including a disk
  set aside mid-update) is deleted after a rebuild or update.
- **Outdated** means the disk's backing file (`qemu-img info -U`) is not the current version.
- **Update, per agent, one at a time:** runs held and cancelled → computer (re)started so it
  runs the current guest software → `carry.pack` on the guest (SQLite files via SQLite's
  backup) → fetched in 2 MB pieces, size and SHA-256 checked → computer stopped, disk renamed
  `disk.previous.qcow2` → started on a fresh disk → archive sent, `carry.unpack` checks the
  SHA-256 and extracts with tarfile's `data` filter, only into `~/Shared`, `~/Databases` and
  `~/.agent-office/memory.sqlite` → old disk and archive deleted. Any failure after the swap
  puts the old disk back. If the app quits mid-update, the next start does the same.
- **Trust:** the archive comes from the agent's computer. The host only sizes and checksums
  it (at most 4 GB, with 2 GB spare on the Mac) and never opens it.

## What is not kept
Programs the agent installed, browser sign-ins, files outside the Shared folder, and snapshots
of the old disk. Chats, settings and permissions live on the Mac and are not affected.
