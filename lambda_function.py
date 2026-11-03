"""AWS Lambda entry point: take one sample of every segment and store it in S3.

Triggered every 15 minutes by EventBridge Scheduler (see aws/main.tf). Each run
writes one small CSV to s3://$BUCKET/raw/date=YYYY-MM-DD/HHMM.csv; fetch_s3.py
merges them back into data/travel_times.csv for analyze.py.
"""

import csv
import io
import os

import boto3

from providers import CSV_FIELDS, sample_rows

s3 = boto3.client("s3")


def handler(event, context):
    now, rows = sample_rows()
    if not rows:
        raise RuntimeError("Every segment failed; see the log for errors.")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_FIELDS)
    w.writerows(rows)
    # now looks like 2026-11-02T08:15:00+05:30
    key = f"raw/date={now[:10]}/{now[11:13]}{now[14:16]}.csv"
    s3.put_object(Bucket=os.environ["BUCKET"], Key=key, Body=buf.getvalue().encode(),
                  ContentType="text/csv")
    return {"key": key, "segments": len(rows)}
