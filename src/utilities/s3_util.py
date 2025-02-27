import itertools
import os

from concurrent.futures import ProcessPoolExecutor

import boto3
from boto3.s3.transfer import TransferConfig

from utilities.log_util import log_command


s3c = boto3.client("s3")
s3r = boto3.resource("s3")
bucket_resource = s3r.Bucket("czbiohub-seqbot")
S3_RETRY = 3


# cribbed from https://github.com/chanzuckerberg/s3mi/blob/master/scripts/s3mi
def s3_bucket_and_key(s3_uri, require_prefix=False):
    """
    Return the bucket and key name as a two-element list, key here could be
    file or folder under the bucket
    """
    prefix = "s3://"
    if require_prefix:
        assert s3_uri.startswith(prefix), "The path must start with s3://"
    elif not s3_uri.startswith(prefix):
        prefix = ""

    return s3_uri[len(prefix) :].split("/", 1)


def get_folders(bucket="czb-seqbot", prefix=None):
    """List the folders under a specific path in a bucket. Prefix should end with a /
    """
    paginator = s3c.get_paginator("list_objects_v2")

    response_iterator = paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/")

    for response_data in response_iterator:
        if "CommonPrefixes" in response_data:
            yield from (c["Prefix"] for c in response_data["CommonPrefixes"])


def prefix_gen(bucket, prefix, fn=None):
    """Generic generator of fn(result) from an S3 paginator"""

    paginator = s3c.get_paginator("list_objects_v2")

    response_iterator = paginator.paginate(Bucket=bucket, Prefix=prefix)

    for result in response_iterator:
        if "Contents" in result:
            yield from (fn(r) for r in result["Contents"])


def get_files(bucket="czb-seqbot", prefix=None):
    """Generator of keys for a given S3 prefix. Be careful that the first element of the generator output is simply the input prefix. Keys of files start from the second element of the generator output."""
    yield from prefix_gen(bucket, prefix, lambda r: r["Key"])


def get_size(bucket="czb-seqbot", prefix=None):
    """Generator of (key,size) for a given S3 prefix. Unlike get_files, the first element of the generator output is not a tuple with input prefix as key and 0 as size, but rather the (key,size) pair for the first file under the input prefix."""
    yield from prefix_gen(bucket, prefix, lambda r: (r["Key"], r["Size"]))


def get_status(file_list, bucket_name="czb-seqbot"):
    """Print the storage/restore status for a list of keys"""

    for fn in file_list:
        obj = s3r.Object(bucket_name, fn)
        print(obj.key, obj.storage_class, obj.restore)


def restore_file(k):
    obj = s3r.Object("czbiohub-seqbot", k)
    if obj.storage_class == "GLACIER" and not obj.restore:
        bucket_resource.meta.client.restore_object(
            Bucket="czbiohub-seqbot", Key=k, RestoreRequest={"Days": 7}
        )


def copy_file(bucket, new_bucket, key, new_key):
    s3c.copy(
        CopySource={"Bucket": bucket, "Key": key},
        Bucket=new_bucket,
        Key=new_key,
        Config=TransferConfig(use_threads=False),
    )


def remove_file(bucket, key):
    s3c.delete_object(Bucket=bucket, Key=key)


def download_file(bucket, key, dest):
    s3c.download_file(
        Bucket=bucket, Key=key, Filename=dest, Config=TransferConfig(use_threads=False)
    )


def restore_files(file_list, *, n_proc=16):
    """Restore a list of files from czbiohub-seqbot in parallel"""

    print(f"restoring {len(file_list)} files")
    with ProcessPoolExecutor(max_workers=n_proc) as executor:
        list(executor.map(restore_file, file_list, chunksize=64))


def copy_files(src_list, dest_list, *, b, nb, force_copy=False, n_proc=16):
    """
    Copy a list of files from src_list to dest_list.
    b - original bucket
    nb - destination bucket
    """

    if not force_copy:
        existing_files = set(
            get_files(bucket=nb, prefix=os.path.commonprefix(dest_list))
        ) & set(dest_list)
        src_list, dest_list = zip(
            *[
                (src, dest)
                for src, dest in zip(src_list, dest_list)
                if dest not in existing_files
            ]
        )

    print(f"copying {len(src_list)} files")
    with ProcessPoolExecutor(max_workers=n_proc) as executor:
        list(
            executor.map(
                copy_file,
                itertools.repeat(b),
                itertools.repeat(nb),
                src_list,
                dest_list,
                chunksize=64,
            )
        )


def remove_files(file_list, *, b, really=False, n_proc=16):
    """Remove a list of file keys from S3"""

    assert really

    print(f"Removing {len(file_list)} files!")
    with ProcessPoolExecutor(max_workers=n_proc) as executor:
        list(executor.map(remove_file, itertools.repeat(b), file_list, chunksize=64))


def download_files(src_list, dest_list, *, bucket, force_download=False, n_proc=16):
    """Download a list of file to local storage"""

    if not force_download:
        src_list, dest_list = zip(
            *[
                (src, dest)
                for src, dest in zip(src_list, dest_list)
                if not os.path.exists(dest)
            ]
        )

    with ProcessPoolExecutor(max_workers=n_proc) as executor:
        list(
            executor.map(
                download_file,
                itertools.repeat(bucket),
                src_list,
                dest_list,
                chunksize=64,
            )
        )


def s3_sync(logger, input, output, retries=S3_RETRY):
    command = [
        "aws",
        "s3",
        "sync",
        "--no-progress",
        input,
        output,
    ]
    for _ in range(retries):
        if not log_command(logger, command, shell=True):
            break
        logger.info(f"retrying sync")
    else:
        raise RuntimeError(f"couldn't sync output")


def s3_cp(logger, input, output, retries=S3_RETRY):
    command = [
        "aws",
        "s3",
        "cp",
        input,
        output,
    ]
    for _ in range(retries):
        if not log_command(logger, command, shell=True):
            break
        logger.info(f"retrying cp")
    else:
        raise RuntimeError(f"couldn't cp output")
