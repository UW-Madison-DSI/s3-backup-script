# Quick S3 Backup Utility

Two scripts, same idea: tar a directory and stream it straight to remote storage without writing a local archive first.

- `backup_to_s3.py` streams to an S3 bucket as a multipart upload.
- `backup_to_research_drive.py` streams to a tar file on [UW ResearchDrive](https://kb.wisc.edu/researchdata/page.php?id=72186) via an SMB mount. See [Backup to ResearchDrive](#3-backup-to-researchdrive).

## 1. Script only
Lightest weight option: use the backup script directly in an existing Python environment:
1. make sure `boto3`, `tqdm`, `certifi`, and `python-dotenv`* are present in your environment:
	```bash
	pip install boto3 tqdm certifi python-dotenv
	```
	\* you don't need python-dotenv if you're not going to use an .env file for your creds
2. copy backup_to_s3.py from [the repo](https://raw.githubusercontent.com/UW-Madison-DSI/s3-backup-script/refs/heads/main/backup_to_s3.py) to a convenient location.
3. Provide S3 credentials. Choose either:
	1. Edit the script and enter creds there directly
	2. ```bash
		cp example.env .env
		```
		edit `.env` and enter your credentials there
3. run the script to backup a directory to the s3 bucket you specified:
	```bash
	python3 backup_to_s3.py <dir_to_backup>
	```
4. Confirm backup completes and files are in expected location.

## 2. Install with pixi
Create a separate Python environment isolated from your other work specifically to run the backup script.

1. [install pixi](https://pixi.prefix.dev/latest/#installation)
	```bash
	curl -fsSL https://pixi.sh/install.sh | sh
	```
2. clone repo
	```bash
	git clone git@github.com:UW-Madison-DSI/s3-backup-script.git
	```
3. Set pixi cache dir (optional)

	This will make Pixi install packages much faster on Olvi by specifying a non-AFS drive for the pixi cache
	```bash
	mkdir -p /data/<username>/.pixi_cache
	export PIXI_CACHE_DIR="/data/<username>/.pixi_cache"
	```
4. install repo
	```bash
	cd s3-backup-script/
	pixi install
	```
5. Provide your s3 credentials in an .env file:
	1. ```bash
		cp example.env .env
		```
	2. edit `.env` and enter your credentials there
6. run script:
	```bash
	pixi run backup-to-s3 <dir_to_backup>
	```
3. confirm backup completes, and files are in expected s3 location

## 3. Backup to ResearchDrive
`backup_to_research_drive.py` writes `<prefix>/<dir_name>.tar` onto a ResearchDrive share. It follows [CSL's ResearchDrive instructions for Linux](https://csl.cs.wisc.edu/docs/csl/researchdrive/): the share is mounted per-user with `gio`, which exposes it as a normal directory under `/run/user/<uid>/gvfs/`.

### Prerequisites
- An **interactive login** on the host (e.g. `ssh` to Olvi). `gio` needs your login session's D-Bus and gvfs daemons; it does not work from cron.
- Run inside `tmux` or `screen`. A gvfs mount belongs to your login session and disappears if that session ends, which would kill a long backup.
- The script only needs `tqdm` and `python-dotenv` (already in the pixi env; or `pip install tqdm python-dotenv`).

### Configure
Add the ResearchDrive settings to `.env` (see `example.env`). `BACKUP_RD_SHARE` is the NetID of the PI who owns the ResearchDrive space; `BACKUP_RD_PREFIX` is an optional directory inside the share.
```bash
BACKUP_RD_HOST="research.drive.wisc.edu"
BACKUP_RD_SHARE="pi_netid"
BACKUP_RD_PREFIX="backups"
```
Your NetID password is **never** stored. `gio` prompts for it once when the share is first mounted.

### Run
```bash
# script only
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

### ResearchDrive troubleshooting
- **`is not mounted and no terminal is available`**: the script cannot prompt for credentials because stdin is not a terminal. Mount the share by hand from an interactive shell first, or set `BACKUP_RD_MOUNT` in `.env` to a directory where the share is already mounted (any mount type works; the script then never calls `gio`).
- **`gio mount` fails or hangs**: check that `gvfsd` is running for your user (`pgrep -u $USER gvfsd`). If not, log out and back in so a fresh session bus starts.


## Troubleshooting

### `SSL validation failed ... CERTIFICATE_VERIFY_FAILED ... self-signed certificate in certificate chain`
boto3 is verifying the S3 endpoint against a stale CA bundle. `botocore` only uses a current Mozilla root store when the `certifi` package is importable; otherwise it falls back to its own vendored `cacert.pem`, which is missing newer roots such as `emSign Root CA - G1` (the root `web.s3.wisc.edu` has chained to since September 2026).

Fix, in order of preference:
1. Install `certifi` in the environment running the script (`pip install certifi`, or `pixi install` after pulling this repo). The script picks it up automatically.
2. Or point the script at your system's CA bundle in `.env`:
	```bash
	BACKUP_S3_CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
	```
	(`AWS_CA_BUNDLE` is honoured by boto3 as well.)
