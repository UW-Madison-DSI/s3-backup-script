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

    pip install boto3
"""

import io
import os
import sys
import time
import tarfile
import argparse


import boto3


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


# S3 multipart uploads require every part except the final part
# to be at least 5 MiB.
PART_SIZE = 64 * 1024 * 1024  # 64 MiB


class S3MultipartWriter(io.RawIOBase):
    """
    File-like object that receives a byte stream and sends it to S3
    as a multipart upload.

    tarfile can write directly to this object, so the complete archive
    never has to exist on local disk.
    """

    def __init__(self, s3, bucket, key, part_size=PART_SIZE):
        self.s3 = s3
        self.bucket = bucket
        self.key = key
        self.part_size = part_size

        response = self.s3.create_multipart_upload(
            Bucket=self.bucket,
            Key=self.key,
            ContentType="application/x-tar",
        )

        self.upload_id = response["UploadId"]

        self.part_number = 1
        self.parts = []
        self.buffer = bytearray()
        self.total_bytes = 0
        self.closed_ = False

    def writable(self):
        return True

    def write(self, data):
        if self.closed_:
            raise ValueError("S3 writer is closed")

        if not data:
            return 0

        self.buffer.extend(data)
        self.total_bytes += len(data)

        while len(self.buffer) >= self.part_size:
            self._upload_part(
                bytes(self.buffer[:self.part_size])
            )

            del self.buffer[:self.part_size]

        return len(data)

    def _upload_part(self, data):
        size_mb = len(data) / (1024 * 1024)

        print(
            f"Uploading part {self.part_number}: "
            f"{size_mb:.1f} MiB",
            flush=True,
        )

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
            # Upload the final part. It is allowed to be smaller
            # than PART_SIZE.
            if self.buffer:
                self._upload_part(
                    bytes(self.buffer)
                )
                self.buffer.clear()

            if not self.parts:
                self._abort()
                raise RuntimeError(
                    "No data was written to the S3 upload"
                )

            print(
                "Completing S3 multipart upload...",
                flush=True,
            )

            self.s3.complete_multipart_upload(
                Bucket=self.bucket,
                Key=self.key,
                UploadId=self.upload_id,
                MultipartUpload={
                    "Parts": self.parts
                },
            )

            self.closed_ = True

        except Exception:
            self._abort()
            raise

    def _abort(self):
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


def create_s3_client():
    """
    Create and return the boto3 S3 client using S3_CONFIG.
    """

    client_args = {
        "endpoint_url": f"https://{S3_CONFIG['host']}",
        "aws_access_key_id": S3_CONFIG["key"],
        "aws_secret_access_key": S3_CONFIG["secret"],
    }

    # Only specify a region if one was configured.
    if S3_CONFIG["region"]:
        client_args["region_name"] = S3_CONFIG["region"]

    return boto3.client("s3", **client_args)


def create_object_key(source):
    """
    Create an S3 object key from the source directory name.

    Example:

        /mnt/backup-source/home/alice/projects

    becomes:

        backups/projects.tar
    """

    source = os.path.abspath(source)

    source_name = os.path.basename(
        os.path.normpath(source)
    )

    prefix = S3_CONFIG["prefix"].strip("/")

    if prefix:
        return f"{prefix}/{source_name}.tar"

    return f"{source_name}.tar"


def backup_directory(source, bucket, key):
    """
    Create a tar archive of 'source' and stream it directly to S3.
    """

    source = os.path.abspath(source)

    if not os.path.isdir(source):
        raise ValueError(
            f"Source is not a directory: {source}"
        )

    print(f"Source: {source}")
    print(f"Destination: s3://{bucket}/{key}")
    print(f"S3 endpoint: https://{S3_CONFIG['host']}")
    print()

    s3 = create_s3_client()

    writer = S3MultipartWriter(
        s3=s3,
        bucket=bucket,
        key=key,
        part_size=PART_SIZE,
    )

    try:
        # "w|" is tar's streaming mode.
        #
        # Unlike normal "w", tarfile does not need to seek backwards
        # through the output archive.
        with tarfile.open(
            fileobj=writer,
            mode="w|",
        ) as tar:

            archive_name = os.path.basename(
                os.path.normpath(source)
            )

            print(
                f"Creating archive directory: "
                f"{archive_name}/",
                flush=True,
            )

            tar.add(
                source,
                arcname=archive_name,
                recursive=True,
            )

    except Exception:
        # If tar fails, make sure the incomplete S3 upload is removed.
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
        description=(
            "Stream a directory into an S3 multipart "
            "upload as a tar archive."
        )
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

