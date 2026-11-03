# Run the collector on AWS

Every 15 minutes from 06:00 to 22:45 IST, EventBridge Scheduler triggers a Lambda function.
The function gets live travel times for all 14 ORR segments from Amazon Location Service
and writes them to S3. Nothing needs to run on your laptop.

```
EventBridge Scheduler --(cron, Asia/Kolkata)--> Lambda --> Amazon Location (geo-routes)
                                                   \--> S3: raw/date=YYYY-MM-DD/HHMM.csv
```

## 1. Prerequisites

```bash
brew install awscli
aws configure            # or: aws configure sso && aws sso login
aws sts get-caller-identity
```

Use an account where you can create IAM roles, Lambda, S3 and EventBridge Scheduler.

## 2. Check the corridor from your laptop first

```bash
cd ~/bengaluru-traffic
.venv/bin/pip install -r requirements.txt
.venv/bin/python collector.py check
```

This makes 14 Amazon Location route calls and confirms that each segment stays on the ORR.
If a segment is flagged, fix its coordinates in `config.py`. The Lambda deploys the same file.

If you get an error saying the service is unavailable in `ap-south-1`, set
`AWS_REGION = "ap-southeast-1"` in `config.py` and pass `-var region=ap-southeast-1` below.

## 3. Deploy

```bash
cd aws
terraform init
terraform plan
terraform apply
```

This creates an S3 bucket, a Lambda function, two IAM roles, a log group and the schedule.
Collection starts at the next 15-minute mark inside the window.

Check that it's working after the first run:

```bash
aws logs tail /aws/lambda/blr-orr-traffic-collector --since 30m
aws s3 ls s3://$(terraform output -raw bucket)/raw/ --recursive | tail
```

## 4. Analyse

```bash
cd ~/bengaluru-traffic
.venv/bin/python fetch_s3.py $(terraform -chdir=aws output -raw bucket)
.venv/bin/python analyze.py
```

## Cost

- About 2,860 route calls per day (20,000 per week): each sample asks for the live time plus the
  provider's own estimate for departures 30 and 60 minutes ahead (the benchmark for our forecast).
  Set `BENCHMARK_HORIZONS_MIN = []` in `config.py` to drop back to ~950/day. Amazon Location is
  charged per route request; check the current price and any free tier on the Amazon Location
  pricing page before running for more than a week.
- Each sample also makes one Open-Meteo call for rain. Its free API is for non-commercial use;
  a public app needs Open-Meteo's paid plan or another weather source.
- Lambda, S3 and the scheduler cost almost nothing at this volume.
- To cut cost, change the schedule in `main.tf` to `cron(0/30 ...)` or collect only peak hours,
  for example `cron(0/15 7-11,16-21 * * ? *)`.

## Pause or remove

```bash
terraform apply -var collection_enabled=false   # pause and keep the data
terraform destroy                                # remove everything (empty the bucket first)
```

## Note on the data

Amazon Location returns live-traffic duration but no free-flow time, so `static_duration_s` is
blank. `analyze.py` doesn't need it: it uses each segment's fastest observed times as the free-flow
baseline.
