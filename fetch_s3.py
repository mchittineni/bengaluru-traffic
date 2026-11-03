"""Download every sample the Lambda collector wrote to S3 into data/travel_times.csv.

Usage:
    python fetch_s3.py <bucket-name>      # bucket name is printed by `terraform output`
"""

import csv
import io
import os
import sys

import boto3

import config
from providers import CSV_FIELDS


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    bucket = sys.argv[1]
    s3 = boto3.client("s3", region_name=config.AWS_REGION)
    rows, files = [], 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="raw/"):
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode()
            # DictReader so files written before new columns were added still line up.
            rows += list(csv.DictReader(io.StringIO(body)))
            files += 1
    rows.sort(key=lambda r: (r["timestamp"], r["direction"], int(r["seg_order"])))
    os.makedirs(os.path.dirname(config.DATA_FILE), exist_ok=True)
    with open(config.DATA_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, CSV_FIELDS, restval="", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Merged {files} samples ({len(rows)} rows) into {config.DATA_FILE}")


if __name__ == "__main__":
    main()
