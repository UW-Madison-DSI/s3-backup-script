#!/usr/bin/env python3

"""
Stream a directory into an S3 multipart upload as a tar archive.

Usage:

    python backup-to-s3.py /mnt/backup-source/home/alice/projects

The destination S3 object key is automatically generated from
the name of the source directory.

For example:

    /mnt/backup-source/home/alice/projects

becomes:

    s3://bucket-name/projects.tar

The source should ideally be a read-only mount of an EBS snapshot.

Requirements:

    pip install boto3 tqdm python-dotenv certifi
"""

import argparse
import io
import os
import ssl
import sys
import tarfile
import time

import boto3
from tqdm import tqdm

# ----------------------------------------------------------------------
# S3 configuration
# ----------------------------------------------------------------------

S3_CONFIG = {
    # S3-compatible endpoint
    "host": "web.s3.wisc.edu",
    # S3 access credentials
    "key": "YOUR_ACCESS_KEY",
    "secret": "YOUR_NEW_SECRET_KEY",
    # Destination bucket
    "bucket": "YOUR_BUCKET_NAME",
    # Directory within the bucket where backups are stored
    "prefix": "",
    # Leave empty if the S3 service does not require a region
    "region": "",
}

# overwrite with values from .env file if it exists
if os.path.exists(".env"):
    from dotenv import load_dotenv

    # Pass the path explicitly: with no argument, load_dotenv() searches from
    # the script's own directory rather than the current working directory
    # that os.path.exists() just checked.
    load_dotenv(".env")
    print(".env file found, using creds in .env file.")

    S3_CONFIG = S3_CONFIG | {
        "host": os.environ.get("BACKUP_S3_HOST"),
        "key": os.environ.get("BACKUP_S3_KEY"),
        "secret": os.environ.get("BACKUP_S3_SECRET"),
        "bucket": os.environ.get("BACKUP_S3_BUCKET"),
        "prefix": os.environ.get("BACKUP_S3_PREFIX"),
        "region": os.environ.get("BACKUP_S3_REGION"),
    }
else:
    print("no .env file found, using creds from script.")


# S3 multipart uploads require every part except the final part >= 5 MiB.
PART_SIZE = 64 * 1024 * 1024  # 64 MiB


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


class S3MultipartWriter(io.RawIOBase):
    """
    File-like object that receives a byte stream and sends it to S3
    as a multipart upload.
    """

    def __init__(self, s3, bucket, key, pbar=None, part_size=PART_SIZE):
        self.s3 = s3
        self.bucket = bucket
        self.key = key
        self.part_size = part_size
        self.pbar = pbar

        # Initialise all state before any network call. io.RawIOBase.__del__
        # calls close() when the object is garbage collected, so close() must
        # be safe to run even if create_multipart_upload() raises below.
        self.upload_id = None
        self.part_number = 1
        self.parts = []
        self.buffer = bytearray()
        self.total_bytes = 0
        self.aborted = False
        self.closed_ = False

        response = self.s3.create_multipart_upload(
            Bucket=self.bucket,
            Key=self.key,
            ContentType="application/x-tar",
        )

        self.upload_id = response["UploadId"]

    def writable(self):
        return True

    def write(self, data):
        if self.closed_:
            raise ValueError("S3 writer is closed")

        if self.aborted:
            raise ValueError("S3 multipart upload was aborted")

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
            self._upload_part(bytes(self.buffer[: self.part_size]))
            del self.buffer[: self.part_size]

        return len(data)

    def _upload_part(self, data):
        response = self.s3.upload_part(
            Bucket=self.bucket,
            Key=self.key,
            UploadId=self.upload_id,
            PartNumber=self.part_number,
            Body=data,
        )
        self.parts.append(
            {
                "PartNumber": self.part_number,
                "ETag": response["ETag"],
            }
        )
        self.part_number += 1

    def close(self):
        if self.closed_:
            return

        try:
            if self.upload_id is None or self.aborted:
                # Nothing to finish: the upload was never created, or it was
                # already aborted (e.g. after a failure while writing the tar).
                return

            if self.buffer:
                self._upload_part(bytes(self.buffer))
                self.buffer.clear()

            if not self.parts:
                self._abort()
                raise RuntimeError("No data was written to the S3 upload")

            tqdm.write("Completing S3 multipart upload...")

            self.s3.complete_multipart_upload(
                Bucket=self.bucket,
                Key=self.key,
                UploadId=self.upload_id,
                MultipartUpload={"Parts": self.parts},
            )

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
        if self.upload_id is None or self.aborted:
            return

        self.aborted = True

        try:
            self.s3.abort_multipart_upload(
                Bucket=self.bucket,
                Key=self.key,
                UploadId=self.upload_id,
            )
        except Exception as e:
            print(
                f"WARNING: failed to abort multipart upload: {e}",
                file=sys.stderr,
            )


def get_ca_bundle():
    """
    Return the path of the CA bundle boto3 should verify TLS against, or
    None to let botocore use its own vendored bundle.

    botocore only uses certifi when it is importable; otherwise it falls
    back to a vendored cacert.pem that can lag behind the Mozilla root
    store. That bundle lacks roots such as "emSign Root CA - G1", which
    web.s3.wisc.edu began chaining to in September 2026.

    Resolution order:
        1. BACKUP_S3_CA_BUNDLE environment variable (explicit override)
        2. certifi's bundle, if installed
        3. the default CA file of the Python/OpenSSL build
        4. None (botocore's vendored bundle)
    """
    override = os.environ.get("BACKUP_S3_CA_BUNDLE")
    if override:
        if not os.path.isfile(override):
            raise FileNotFoundError(
                f"BACKUP_S3_CA_BUNDLE points to a missing file: {override}"
            )
        return override

    try:
        import certifi

        return certifi.where()
    except ImportError:
        pass

    paths = ssl.get_default_verify_paths()
    for candidate in (paths.cafile, paths.openssl_cafile):
        if candidate and os.path.isfile(candidate):
            return candidate

    return None


def create_s3_client():
    client_args = {
        "endpoint_url": f"https://{S3_CONFIG['host']}",
        "aws_access_key_id": S3_CONFIG["key"],
        "aws_secret_access_key": S3_CONFIG["secret"],
    }

    if S3_CONFIG["region"]:
        client_args["region_name"] = S3_CONFIG["region"]

    ca_bundle = get_ca_bundle()
    if ca_bundle:
        client_args["verify"] = ca_bundle

    return boto3.client("s3", **client_args)


def create_object_key(source):
    source = os.path.abspath(source)
    source_name = os.path.basename(os.path.normpath(source))
    prefix = S3_CONFIG["prefix"].strip("/")

    if prefix:
        return f"{prefix}/{source_name}.tar"

    return f"{source_name}.tar"


def backup_directory(source, bucket, key):
    source = os.path.abspath(source)

    if not os.path.isdir(source):
        raise ValueError(f"Source is not a directory: {source}")

    print(f"Source: {source}")
    print(f"Destination: s3://{bucket}/{key}")
    print(f"S3 endpoint: https://{S3_CONFIG['host']}")
    print("Calculating source directory size...")

    total_size = get_directory_size(source)
    s3 = create_s3_client()
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
        writer = S3MultipartWriter(
            s3=s3,
            bucket=bucket,
            key=key,
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
            # abort, otherwise the finally below would complete a truncated
            # multipart upload as if it were the whole archive.
            writer._abort()
            raise

        finally:
            writer.close()

    total_mb = writer.total_bytes / (1024 * 1024)
    total_gb = total_mb / 1024

    print()
    print("Backup complete.")
    print(f"S3 object: s3://{bucket}/{key}")
    print(f"Archive size: {total_gb:.2f} GiB")


def main():
    parser = argparse.ArgumentParser(
        description=("Stream a directory into an S3 multipart upload as a tar archive.")
    )

    parser.add_argument(
        "source",
        help="Directory to back up",
    )

    args = parser.parse_args()

    bucket = S3_CONFIG["bucket"]
    key = create_object_key(args.source)

    start = time.perf_counter()
    try:
        backup_directory(
            source=args.source,
            bucket=bucket,
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
