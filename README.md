# Disney Parks Watchtower

A serverless ride-status alerter for Walt Disney World parks. Polls
themeparks.wiki every 2 minutes, diffs against DynamoDB, and fires
Pushover alerts to subscribers when monitored rides go down, come
back up, or stay down for an unusually long time.

Phase 1 (this commit): backend only. The poller runs, alerts fire,
subscribers are seeded manually. Phase 2 will add a Next.js dashboard
on `disney.megillini.dev` with Cognito + Google sign-in for self-service
park toggles.

## Architecture (M1)

```
EventBridge schedule (every 2 min)
    │
    ▼
Poller Lambda (Python 3.12)
    ├── GET themeparks.wiki/v1/entity/<park>/live   (×4 parks)
    ├── DynamoDB: read prior STATE, write new STATE + HIST
    ├── DynamoDB: track DOWN_SINCE + alert COOLDOWN
    └── For each subscriber × event:
            POST api.pushover.net/1/messages.json
```

Single DynamoDB table `DisneyData` holds everything. Schema:

| PK              | SK                    | Purpose                              |
|-----------------|-----------------------|--------------------------------------|
| `RIDE#<id>`     | `STATE`               | Current ride state                   |
| `RIDE#<id>`     | `HIST#<iso_ts>`       | Status change history (90d TTL)      |
| `RIDE#<id>`     | `DOWN_SINCE`          | When the ride went down              |
| `RIDE#<id>`     | `COOLDOWN#DOWN`       | DOWN alert cooldown (15m TTL)        |
| `RIDE#<id>`     | `COOLDOWN#STILL_DOWN` | Second-alert cooldown (45m TTL)      |
| `USER#<id>`     | `PROFILE`             | name + pushover_user_key             |
| `PARK#<key>`    | `USER#<id>`           | Subscription (fanout target)         |

## Prerequisites

- AWS account with CDK bootstrapped in `us-east-2` (already done for
  Watchtower in this account)
- Node.js 20+ and `pnpm` (or `npm`)
- Python 3.12 on PATH (used for local Lambda bundling — Docker
  fallback works if not)
- Pushover account + an "application" registered for Disney alerts
  (https://pushover.net/apps/build) — note the **App Token**
- Pushover **User Key** for each subscriber

## One-time setup

### 1. Install CDK deps

```bash
cd infra
npm install
```

### 2. Seed the Pushover credentials in SSM

The Lambda reads these from Parameter Store at cold start. Bootstrapped
manually so secrets never live in CDK or git.

```bash
# App token (the one Pushover gave you when you registered the app)
aws ssm put-parameter \
  --profile watchtower \
  --region us-east-2 \
  --name /disney/pushover/app_token \
  --type SecureString \
  --value '<your-disney-app-token>'

# Megan's user key (the per-recipient key, not the app token)
aws ssm put-parameter \
  --profile watchtower \
  --region us-east-2 \
  --name /disney/pushover/megan_user_key \
  --type SecureString \
  --value '<megan-pushover-user-key>'
```

### 3. Deploy the stack

```bash
cd infra
npx cdk deploy --profile watchtower
```

Outputs include the table name and Lambda function name — copy them
for the next step.

### 4. Seed your user profile + a park subscription

Until M2 ships the UI, do this with the AWS CLI. Replace the user_key
value with the one you stored in SSM (or any other valid Pushover key).

```bash
# Create your user profile
aws dynamodb put-item \
  --profile watchtower \
  --region us-east-2 \
  --table-name DisneyData \
  --item '{
    "PK":   {"S": "USER#megan"},
    "SK":   {"S": "PROFILE"},
    "name": {"S": "Megan"},
    "pushover_user_key": {"S": "<megan-pushover-user-key>"}
  }'

# Subscribe yourself to Magic Kingdom alerts
aws dynamodb put-item \
  --profile watchtower \
  --region us-east-2 \
  --table-name DisneyData \
  --item '{
    "PK": {"S": "PARK#magic_kingdom"},
    "SK": {"S": "USER#megan"},
    "subscribed_at": {"S": "2026-05-04T00:00:00Z"}
  }'
```

To subscribe to all 4 parks at once:

```bash
for park in magic_kingdom epcot hollywood_studios animal_kingdom; do
  aws dynamodb put-item \
    --profile watchtower --region us-east-2 \
    --table-name DisneyData \
    --item "{
      \"PK\": {\"S\": \"PARK#${park}\"},
      \"SK\": {\"S\": \"USER#megan\"},
      \"subscribed_at\": {\"S\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}
    }"
done
```

To unsubscribe from a park:

```bash
aws dynamodb delete-item \
  --profile watchtower --region us-east-2 \
  --table-name DisneyData \
  --key '{"PK": {"S": "PARK#magic_kingdom"}, "SK": {"S": "USER#megan"}}'
```

## Verification

### Trigger a manual poll

```bash
aws lambda invoke \
  --profile watchtower --region us-east-2 \
  --function-name <PollerFunctionName-from-cdk-output> \
  --cli-binary-format raw-in-base64-out \
  --payload '{}' \
  /tmp/disney-poll.json && cat /tmp/disney-poll.json
```

Expect a JSON response with `parks_polled`, `changes`, `alerts_sent`,
and `elapsed_secs`. First poll has many "changes" because the table
starts empty (every ride is a new state). Subsequent polls show only
real transitions.

### Tail the logs live

```bash
aws logs tail /aws/lambda/<PollerFunctionName> \
  --profile watchtower --region us-east-2 --follow
```

### Inspect current ride state

```bash
aws dynamodb scan \
  --profile watchtower --region us-east-2 \
  --table-name DisneyData \
  --filter-expression 'SK = :sk' \
  --expression-attribute-values '{":sk": {"S": "STATE"}}' \
  --max-items 5
```

## Cost expectation

| Item | Cost |
|---|---|
| Lambda invocations (~22k/mo at 2-min cadence) | $0 (free tier) |
| DynamoDB on-demand (~50k req/mo) | <$0.10 |
| EventBridge | $0 |
| CloudWatch logs | <$0.10 |
| Pushover | $5 one-time (already paid) |
| **Total recurring** | **~$0.20/mo** |

## What's next (M2 preview)

- Next.js dashboard at `disney.megillini.dev` on AWS Amplify
- Cognito + Google sign-in (reuses Watchtower's user pool via a
  second app client — no Google Cloud changes needed)
- Self-service per-user park toggles + Pushover key management
- Trip-aware auto-toggle (parks light up automatically on trip dates)

See `PROJECT.md` for the full roadmap.
