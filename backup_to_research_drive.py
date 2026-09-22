#!/usr/bin/env python3

"""
Stream a directory into a tar archive on UW ResearchDrive.

Usage:

    python backup_to_research_drive.py /mnt/backup-source/home/alice/projects

The destination file name is automatically generated from the name of the
source directory.

For example:

    /mnt/backup-source/home/alice/projects

becomes:

    smb://research.drive.wisc.edu/<pi netid>/<prefix>/projects.tar

ResearchDrive is reached through a per-user gvfs mount, as recommended by
CSL for Linux hosts (https://csl.cs.wisc.edu/docs/csl/researchdrive/):

    gio mount smb://research.drive.wisc.edu/<pi netid>

If the share is not already mounted, this script runs that command for you
and gio prompts for your NetID credentials (domain AD.WISC.EDU). The
password is never stored. The mount is left in place afterwards.

The source should ideally be a read-only mount of an EBS snapshot.

Requirements:

    pip install tqdm python-dotenv
"""

import argparse
import glob
import io
import os
import shutil
import subprocess
import sys
import tarfile
import time

from tqdm import tqdm

# ----------------------------------------------------------------------
# ResearchDrive configuration
# ----------------------------------------------------------------------

RD_CONFIG = {
    # SMB server
    "host": "research.drive.wisc.edu",
    # Share name: the NetID of the PI who owns the ResearchDrive space
    "share": "YOUR_PI_NETID",
    # Directory within the share where backups are stored
    "prefix": "",
    # Optional explicit mount point. When set, the share is assumed to be
    # mounted here already (e.g. a CIFS mount) and gio is not used.
    "mount": "",
}

# overwrite with values from .env file if it exists
if os.path.exists(".env"):
    from dotenv import load_dotenv

    # Pass the path explicitly: with no argument, load_dotenv() searches from
    # the script's own directory rather than the current working directory
    # that os.path.exists() just checked.
    load_dotenv(".env")
    print(".env file found, using settings in .env file.")

    _ENV_KEYS = {
        "host": "BACKUP_RD_HOST",
        "share": "BACKUP_RD_SHARE",
        "prefix": "BACKUP_RD_PREFIX",
        "mount": "BACKUP_RD_MOUNT",
    }

    # Only override keys that are actually set, so a .env holding only the
    # S3 variables does not wipe the defaults above.
    for _key, _var in _ENV_KEYS.items():
        _value = os.environ.get(_var)
        if _value is not None:
            RD_CONFIG[_key] = _value
else:
    print("no .env file found, using settings from script.")

# The Windows domain gio must be given when it prompts for credentials.
AD_DOMAIN = "AD.WISC.EDU"

# Bytes accumulated before each write to the mount. Large sequential
# writes perform far better over SMB/FUSE than tar's 10 KiB records.
PART_SIZE = 64 * 1024 * 1024  # 64 MiB

# Buffer size for the underlying file handle.
FILE_BUFFER_SIZE = 8 * 1024 * 1024  # 8 MiB

# How long to wait for the gvfs FUSE path to appear after `gio mount`.
MOUNT_WAIT_SECONDS = 10


def get_directory_size(path):
    """Calculate the total size of all files in a directory tree in bytes."""
    total_size = 0
    for root, _, files in os.walk(path):
        for file in files:
            file_path = os.path.join(root, file)
            if not os.path.islink(file_path):
                try:
                    total_size += os.path.getsize(file_path)
                except OSError:
                    pass
    return total_size


class ResearchDriveWriter(io.RawIOBase):
    """
    File-like object that receives a byte stream and writes it to a file
    on the ResearchDrive mount.

    Data is written to ``<dest>.partial`` and renamed to ``<dest>`` only
    when the stream completes successfully, so a half-written archive is
    never left behind under the final name.
    """

    def __init__(self, dest_path, pbar=None, part_size=PART_SIZE):
        self.final_path = dest_path
        self.partial_path = dest_path + ".partial"
        self.part_size = part_size
        self.pbar = pbar

        # Initialise all state before any I/O. io.RawIOBase.__del__ calls
        # close() when the object is garbage collected, so close() must be
        # safe to run even if open() raises below.
        self.file = None
        self.buffer = bytearray()
        self.total_bytes = 0
        self.aborted = False
        self.closed_ = False

        os.makedirs(os.path.dirname(self.partial_path) or ".", exist_ok=True)

        self.file = open(self.partial_path, "wb", buffering=FILE_BUFFER_SIZE)

    def writable(self):
        return True

    def write(self, data):
        if self.closed_:
            raise ValueError("ResearchDrive writer is closed")

        if self.aborted:
            raise ValueError("ResearchDrive write was aborted")

        if not data:
            return 0

        self.buffer.extend(data)
        self.total_bytes += len(data)

        # ensure bar never goes past self.pbar.total from tar overhead
        if self.pbar:
            increment = len(data)
            if self.pbar.total is not None:
                remaining = self.pbar.total - self.pbar.n
                increment = min(increment, max(remaining, 0))
            self.pbar.update(increment)

        while len(self.buffer) >= self.part_size:
            self._write_part(bytes(self.buffer[: self.part_size]))
            del self.buffer[: self.part_size]

        return len(data)

    def _write_part(self, data):
        view = memoryview(data)
        while view:
            written = self.file.write(view)
            if written is None:
                # BufferedWriter always returns a count; guard anyway.
                written = len(view)
            view = view[written:]

    def close(self):
        if self.closed_:
            return

        try:
            if self.file is None or self.aborted:
                # Nothing to finish: the file was never opened, or the
                # write was already aborted (e.g. after a tar failure).
                return

            if self.buffer:
                self._write_part(bytes(self.buffer))
                self.buffer.clear()

            if self.total_bytes == 0:
                self._abort()
                raise RuntimeError("No data was written to the archive")

            tqdm.write("Finalising archive on ResearchDrive...")

            self.file.flush()
            try:
                os.fsync(self.file.fileno())
            except OSError:
                # gvfs/FUSE mounts may not support fsync; the flush above
                # has already handed the data to the SMB layer.
                pass
            self.file.close()

            os.replace(self.partial_path, self.final_path)

            # Snap the bar to 100% for a clean finish. The raw directory
            # size slightly undercounts the tar stream, so the bar can
            # otherwise stop just short of full.
            if self.pbar and self.pbar.total is not None:
                self.pbar.update(self.pbar.total - self.pbar.n)

        except BaseException:
            self._abort()
            raise

        finally:
            # Mark closed even on failure so __del__ does not retry close().
            self.closed_ = True
            super().close()

    def _abort(self):
        if self.file is None or self.aborted:
            return

        self.aborted = True

        try:
            if not self.file.closed:
                self.file.close()
        except OSError as e:
            print(
                f"WARNING: failed to close partial archive: {e}",
                file=sys.stderr,
            )

        try:
            os.remove(self.partial_path)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(
                f"WARNING: failed to remove partial archive {self.partial_path}: {e}",
                file=sys.stderr,
            )


def smb_url():
    return f"smb://{RD_CONFIG['host']}/{RD_CONFIG['share']}"


def gvfs_root():
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime_dir, "gvfs")


def find_gvfs_mount():
    """
    Return the FUSE path of the gvfs SMB mount for the configured share,
    or None if it is not mounted.

    gvfs names the directory ``smb-share:server=<host>,share=<share>`` and
    appends ``,user=...`` / ``,domain=...`` when the mount was created with
    those in the URL, so match on the prefix rather than the exact name.
    """
    pattern = os.path.join(
        gvfs_root(),
        glob.escape(f"smb-share:server={RD_CONFIG['host']},share={RD_CONFIG['share']}")
        + "*",
    )

    for candidate in sorted(glob.glob(pattern)):
        try:
            os.listdir(candidate)
        except OSError:
            continue
        return candidate

    return None


def mount_instructions():
    return (
        "Mount ResearchDrive first, from an interactive login on this host:\n"
        f"    gio mount {smb_url()}\n"
        f"(user = your NetID, domain = {AD_DOMAIN}, password = NetID password)\n"
        "or point BACKUP_RD_MOUNT at an existing mount of the share."
    )


def ensure_mounted():
    """
    Return the local directory through which the ResearchDrive share is
    reachable, mounting it with gio if necessary.
    """
    explicit = RD_CONFIG["mount"]
    if explicit:
        if not os.path.isdir(explicit):
            raise FileNotFoundError(f"BACKUP_RD_MOUNT is not a directory: {explicit}")
        return explicit

    mount = find_gvfs_mount()
    if mount:
        return mount

    if not sys.stdin.isatty():
        raise RuntimeError(
            f"{smb_url()} is not mounted and no terminal is available for "
            f"gio to prompt for credentials.\n{mount_instructions()}"
        )

    print(f"{smb_url()} is not mounted; mounting with gio.")
    print(f"When prompted: user = your NetID, domain = {AD_DOMAIN}.")

    try:
        result = subprocess.run(["gio", "mount", smb_url()])
    except FileNotFoundError:
        raise RuntimeError(
            f"'gio' was not found on this host.\n{mount_instructions()}"
        ) from None

    if result.returncode != 0:
        raise RuntimeError(
            f"gio mount exited with status {result.returncode}.\n{mount_instructions()}"
        )

    # The FUSE path can lag slightly behind `gio mount` returning.
    deadline = time.monotonic() + MOUNT_WAIT_SECONDS
    while True:
        mount = find_gvfs_mount()
        if mount:
            return mount
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    raise RuntimeError(
        f"gio reported success but no gvfs mount appeared under {gvfs_root()} "
        f"within {MOUNT_WAIT_SECONDS} seconds.\n{mount_instructions()}"
    )


def create_object_key(source):
    source = os.path.abspath(source)
    source_name = os.path.basename(os.path.normpath(source))
    prefix = RD_CONFIG["prefix"].strip("/")

    if prefix:
        return f"{prefix}/{source_name}.tar"

    return f"{source_name}.tar"


def warn_if_low_space(mount_root, required_bytes):
    """
    Print a warning if the mount reports less free space than the source
    size. Advisory only: FUSE/SMB statvfs figures are not always reliable.
    """
    try:
        usage = shutil.disk_usage(mount_root)
    except OSError:
        return

    if usage.free and usage.free < required_bytes:
        free_gb = usage.free / (1024**3)
        need_gb = required_bytes / (1024**3)
        print(
            f"WARNING: ResearchDrive reports {free_gb:.2f} GiB free but the "
            f"source is {need_gb:.2f} GiB. The backup may fail.",
            file=sys.stderr,
        )


def backup_directory(source, mount_root, key):
    source = os.path.abspath(source)

    if not os.path.isdir(source):
        raise ValueError(f"Source is not a directory: {source}")

    dest_path = os.path.join(mount_root, *key.split("/"))

    print(f"Source: {source}")
    print(f"Destination: {smb_url()}/{key}")
    print(f"Local path: {dest_path}")
    print("Calculating source directory size...")

    total_size = get_directory_size(source)
    warn_if_low_space(mount_root, total_size)
    bar_format = "{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]"

    with tqdm(
        total=total_size,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc="Streaming backup",
        bar_format=bar_format,
        dynamic_ncols=True,
    ) as pbar:
        writer = ResearchDriveWriter(
            dest_path=dest_path,
            pbar=pbar,
            part_size=PART_SIZE,
        )

        try:
            with tarfile.open(
                fileobj=writer,
                mode="w|",
            ) as tar:
                archive_name = os.path.basename(os.path.normpath(source))

                tar.add(
                    source,
                    arcname=archive_name,
                    recursive=True,
                )

        except BaseException:
            # BaseException, not Exception: a KeyboardInterrupt must also
            # abort, otherwise the finally below would finalise a truncated
            # archive under the real name.
            writer._abort()
            raise

        finally:
            writer.close()

    total_mb = writer.total_bytes / (1024 * 1024)
    total_gb = total_mb / 1024

    print()
    print("Backup complete.")
    print(f"ResearchDrive file: {smb_url()}/{key}")
    print(f"Local path: {dest_path}")
    print(f"Archive size: {total_gb:.2f} GiB")


def main():
    parser = argparse.ArgumentParser(
        description=("Stream a directory into a tar archive on UW ResearchDrive.")
    )

    parser.add_argument(
        "source",
        help="Directory to back up",
    )

    args = parser.parse_args()

    if not RD_CONFIG["share"] or RD_CONFIG["share"] == "YOUR_PI_NETID":
        print(
            "ResearchDrive share is not configured. Set BACKUP_RD_SHARE in .env "
            "(or edit RD_CONFIG in the script) to the PI's NetID.",
            file=sys.stderr,
        )
        sys.exit(2)

    key = create_object_key(args.source)

    start = time.perf_counter()
    try:
        mount_root = ensure_mounted()
        backup_directory(
            source=args.source,
            mount_root=mount_root,
            key=key,
        )

    except KeyboardInterrupt:
        print(
            "\nBackup interrupted.",
            file=sys.stderr,
        )
        sys.exit(130)

    except Exception as e:
        print(
            f"\nBackup failed: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    end = time.perf_counter()
    elapsed = end - start
    print(f"Backup completed in {elapsed:.4f} seconds.")


if __name__ == "__main__":
    main()
