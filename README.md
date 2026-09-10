# Quick S3 Backup Utility

## 1. Install
Either use the script directly in your existing python environment or do the following:

1. [install pixi](https://pixi.prefix.dev/latest/#installation)
```bash
curl -fsSL https://pixi.sh/install.sh | sh
```
2. clone repo
```bash
git clone git@github.com:UW-Madison-DSI/s3-backup-script.git
```
3. install repo
```bash
cd s3-backup-script/
pixi install
```

## 2. back up data
1. edit script to include your s3 credentials
2. run script
	1. directly in your existing python environment:
	```bash
	python3 s3_backup_script.py <path_to_dir_to_backup>
	```
	2. or if you installed via pixi:
	```bash
	pixi run backup-to-s3 <path_to_dir_to_backup>
	```
3. confirm backup completes, and files are in expected s3 location

