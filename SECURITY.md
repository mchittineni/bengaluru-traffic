# Security policy

## Reporting a vulnerability

Please report security issues privately, not in a public issue: use **Security -> Report a
vulnerability** on this repository's GitHub page. Include what is affected, how to reproduce it,
and the impact you expect. You should get a reply within a week.

## Scope

- The AWS collector in `aws/` (Lambda, IAM roles, S3 bucket, scheduler).
- The 3D viewer in `viewer/` and the GitHub Actions workflows in `.github/`.
- Dependency vulnerabilities that affect how this project uses the dependency.

## Keeping your own deployment safe

- Never commit credentials. AWS access comes from your local AWS profile; the Google key comes from
  the `GOOGLE_MAPS_API_KEY` environment variable. `data/`, `output/` and Terraform state are
  git-ignored because they can hold account details.
- Restrict a Google API key to the Routes API, and give the AWS profile you deploy with only the
  permissions in `aws/main.tf`.
- Collected travel-time data is written to a private S3 bucket that only accepts TLS connections.
