# Quick S3 Backup Utility

## 1. Script only
Lightest weight option: use the backup script directly in an existing Python environment:
1. make sure `boto3`, `tqdm`, and `python-dotenv`* are present in your environment:
	```bash
	pip install boto3 tqdm python-dotenv
	```
	\* you don't need python-dotenv if you're not going to use an .env file for your creds
2. copy s3_backup_script.py from [the repo](https://raw.githubusercontent.com/UW-Madison-DSI/s3-backup-script/refs/heads/main/backup_to_s3.py) to a convenient location.
3. Provide S3 credentials. Choose either:
	1. Edit the script and enter creds there directly
	2. ```bash
		cp example.env .env
		```
		edit `.env` and enter your credentials there
3. run the script to backup a directory to the s3 bucket you specified:
	```bash
	python3 s3_backup_script.py <dir_to_backup>
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

