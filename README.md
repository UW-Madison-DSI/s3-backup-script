# Quick Backup Utility - to S3 or Research Drive

Tar a directory and stream it straight to remote storage without writing a local archive.

- `backup_to_s3.py` streams to an S3 bucket as a multipart upload.
- `backup_to_research_drive.py` streams to a tar file on [UW ResearchDrive](https://kb.wisc.edu/researchdata/page.php?id=72186) via an SMB mount.

Contents:
1. [Installation](#1-installation)
2. [Back up to S3](#2-back-up-to-s3)
3. [Back up to ResearchDrive](#3-back-up-to-researchdrive)
4. [Troubleshooting](#4-troubleshooting)

## 1. Installation

There are two ways to install: A) use the scripts directly in an existing Python environment, B) or use pixi to create an isolated environment.

Either way, you provide credentials in one of two ways:
- Edit the script and enter your credentials directly, **or**
- ```bash
	cp example.env .env
	```
	then edit `.env` and enter your credentials there.

A single `.env` holds settings for both S3 an ResearchDrive (see `example.env`). Each usage section below lists the keys it needs.

### A. Scripts directly (pip)

This is the lightest weight option: run a backup script directly in an existing Python environment.

1. Make sure `boto3`, `tqdm`, `certifi`\*, and `python-dotenv`\*\* are present in your environment:
	```bash
	pip install boto3 tqdm certifi python-dotenv
	```
	\* you don't need `certifi` if your local boto3 TLS certs are up to date.

	\*\* you don't need `python-dotenv` if you're not going to use an `.env` file for your creds. `backup_to_research_drive.py` only needs `tqdm` and `python-dotenv`.
2. Copy the script you need to a convenient location, e.g. `backup_to_s3.py` from [the repo](https://raw.githubusercontent.com/UW-Madison-DSI/s3-backup-script/refs/heads/main/backup_to_s3.py).
3. Provide credentials (see above), then run the script (see [section 2](#2-back-up-to-s3) or [section 3](#3-back-up-to-researchdrive)).

### B. pixi

This method creates a separate Python environment, isolated from your other work, specifically to run the backup scripts. This makes both `backup-to-s3` and `backup-to-research-drive` available via `pixi run`.

1. [install pixi](https://pixi.prefix.dev/latest/#installation)
	```bash
	curl -fsSL https://pixi.sh/install.sh | sh
	```
2. Clone the repo
	```bash
	git clone git@github.com:UW-Madison-DSI/s3-backup-script.git
	```
3. Set pixi cache dir (optional)

	This makes Pixi install packages much faster on Olvi by specifying a non-AFS drive for the pixi cache:
	```bash
	mkdir -p /data/<username>/.pixi_cache
	export PIXI_CACHE_DIR="/data/<username>/.pixi_cache"
	```
4. Install the repo
	```bash
	cd s3-backup-script/
	pixi install
	```
5. Provide credentials (see above), then run a backup (see [section 2](#2-back-up-to-s3) or [section 3](#3-back-up-to-researchdrive)).

## 2. Back up to S3 (fastest, preferred)

`backup_to_s3.py` streams a directory into an S3 bucket as a multipart upload.

### Configure

Set the S3 keys in `.env` (see `example.env`):
```bash
BACKUP_S3_HOST="web.s3.wisc.edu"
BACKUP_S3_KEY="my_s3_key_alphanumeric"
BACKUP_S3_SECRET="my_s3_secret_alphanumeric"
BACKUP_S3_BUCKET="my-bucket-name"
BACKUP_S3_PREFIX="my-dir-in-that-bucket"
BACKUP_S3_REGION=""
```

### Run
NB: run this in a `tmux` session so an interrupted `ssh` connection from your local doesn't abort the backup.

```bash
# scripts directly
python3 backup_to_s3.py <dir_to_backup>

# pixi
pixi run backup-to-s3 <dir_to_backup>
```

Confirm the backup completes and the objects land in the expected bucket/prefix.

## 3. Back up to ResearchDrive (if S3 not an option)

`backup_to_research_drive.py` writes `<prefix>/<dir_name>.tar` onto a ResearchDrive share. It follows [CSL's ResearchDrive instructions for Linux](https://csl.cs.wisc.edu/docs/csl/researchdrive/): the share is mounted per-user with `gio`, which exposes it as a normal directory under `/run/user/<uid>/gvfs/`.

### Configure
Add the ResearchDrive settings to `.env` (see `example.env`). `BACKUP_RD_SHARE` is the NetID of the PI who owns the ResearchDrive space; `BACKUP_RD_PREFIX` is an optional directory inside the share.
```bash
BACKUP_RD_HOST="research.drive.wisc.edu"
BACKUP_RD_SHARE="pi_netid"
BACKUP_RD_PREFIX="backups"
```
Your NetID password is not stored. `gio` prompts for it once when the share is first mounted.

### Run
NB: run this in a `tmux` session so an interrupted `ssh` connection from your local doesn't abort the backup.

```bash
# scripts directly
python3 backup_to_research_drive.py <dir_to_backup>

# pixi
pixi run backup-to-research-drive <dir_to_backup>
```
If the share is not yet mounted, the script runs `gio mount` and you will be prompted for:
- **User**: your NetID
- **Domain**: `AD.WISC.EDU`
- **Password**: your NetID password

The mount is left in place after the backup, so later runs in the same session do not prompt again. The archive is written as `<name>.tar.partial` and renamed to `<name>.tar` only when the stream completes, so an interrupted run never leaves a half-written file under the final name.

### Managing the mount by hand
```bash
gio mount smb://research.drive.wisc.edu/<pi netid>      # mount
gio mount --list                                        # show mounts
gio mount -u smb://research.drive.wisc.edu/<pi netid>   # unmount
```
The mounted path is `/run/user/$UID/gvfs/smb-share:server=research.drive.wisc.edu,share=<pi netid>`.

## 4. Troubleshooting

### S3: `SSL validation failed ... CERTIFICATE_VERIFY_FAILED ... self-signed certificate in certificate chain`
boto3 is verifying the S3 endpoint against a stale CA bundle. `botocore` only uses a current Mozilla root store when the `certifi` package is importable; otherwise it falls back to its own vendored `cacert.pem`, which is missing newer roots such as `emSign Root CA - G1` (the root `web.s3.wisc.edu` has chained to since September 2026).

Fix, in order of preference:
1. Install `certifi` in the environment running the script (`pip install certifi`, or `pixi install` after pulling this repo). The script picks it up automatically.
2. Or point the script at your system's CA bundle in `.env`:
	```bash
	BACKUP_S3_CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
	```
	(`AWS_CA_BUNDLE` is honoured by boto3 as well.)

### ResearchDrive: `is not mounted and no terminal is available`
The script cannot prompt for credentials because stdin is not a terminal. Mount the share by hand from an interactive shell first, or set `BACKUP_RD_MOUNT` in `.env` to a directory where the share is already mounted (any mount type works; the script then never calls `gio`).

### ResearchDrive: `gio mount` fails or hangs
Check that `gvfsd` is running for your user (`pgrep -u $USER gvfsd`). If not, log out and back in so a fresh session bus starts.
