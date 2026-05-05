# Magic Monitor — Runbook

Operational reference for Magic Monitor. Read this when:
- Picking the project up after a break
- Debugging a deployment failure
- Onboarding another engineer or AI agent
- Comparing this project's architecture against Watchtower's

`PROJECT.md` is the roadmap (what's done / what's next).
`README.md` is the user-facing setup guide.
This file is the **operational layer** — environment particulars,
hard-won lessons, and the runtime architecture as it actually exists.

## Quick reference

| | Value |
|---|---|
| Live URL | https://magicmonitor.megillini.dev |
| Default Amplify URL | https://main.d1ykat3qyev5c8.amplifyapp.com |
| AWS account | 601669029997 |
| AWS region | us-east-2 |
| AWS CLI profile | `watchtower` (SSO — refresh with `aws sso login --profile watchtower`) |
| GitHub repo | https://github.com/illinigirl/MagicMonitor (private) |
| CFN stack name | `DisneyStack` |
| Cognito user pool | `us-east-2_ORhu761AY` (owned by Watchtower stack, imported here) |
| Cognito hosted-UI | https://auth.megillini.dev (owned by Watchtower stack, reused here) |
| MM Cognito app client | `7m45buvekqqjt8c9vfmtbrfld4` |
| Amplify app id | `d1ykat3qyev5c8` |
| DDB table | `DisneyData` |
| Poller fn name | `DisneyStack-PollerFunction3245213F-WGPWqKXc45P4` |
| Poller schedule | every 2 min, EventBridge rule `disney-poller-every-2min` |
| Pushover SSM | `/disney/pushover/{app_token,megan_user_key}` |

## Architecture as deployed (post-M2-B)

```
                    Cloudflare DNS
                          │
                          ▼
                    CloudFront (Amplify-managed, us-east-1 cert)
                          │
                          ▼
   ┌───────────────────  Amplify SSR Lambda  ──────────────────┐
   │                  (Compute-d1ykat3qyev5c8-...)             │
   │                  (us-east-2, Node.js 22)                  │
   │                                                           │
   │  Server Components (e.g. /parks/[park])                   │
   │     ├── reads DisneyData via @aws-sdk/lib-dynamodb        │
   │     │   (compute role auto-created by L2)                 │
   │     └── reads themeparks.wiki for park hours              │
   │                                                           │
   │  NextAuth handlers (/api/auth/*)                          │
   │     ├── Cognito provider (issuer = us-east-2_ORhu761AY)   │
   │     └── Google IdP behind Cognito (no Google Cloud setup) │
   └───────────────────────────────────────────────────────────┘
                          │
                          ▼
                     DynamoDB DataTable (DisneyData, us-east-2)
                          ▲
                          │
                ┌─────────┴─────────┐
                │  Poller Lambda    │
                │  (Python 3.12)    │
                │  every 2 min      │
                └───────────────────┘
```

**Single-tier read pattern** is deliberate (vs Watchtower's APIGW + FastAPI):
- Server Components scan DDB directly through the Amplify SSR compute role
- M3 writes will be Next.js Route Handlers in the same app (NOT a separate FastAPI Lambda)
- Trade-off: we lose blast-radius separation between read and write paths;
  the SSR compute role grows broader in M3. For a portfolio app at this
  scale, simplicity wins. See `web/src/lib/dynamodb.ts` for the read
  implementation and PROJECT.md "Architecture note" under M2-B.

## M2-B journey — what worked, what didn't, and the lessons

Shipped 2026-05-05. ~7 hours of debugging vs the ~3 hours estimated, almost
entirely because of the issues below. None of these are obvious from AWS
docs or CDK alpha-module signatures.

### Lesson 1: Amplify Hosting + new GitHub apps require manual GitHub App install

**Symptom:** every Amplify build for MM failed in <1 second with
`Unable to assume specified IAM Role`. Watchtower's identical-pattern
build succeeded the same day. Tried clearing roles, recreating roles,
matching trust policies exactly — none of it helped.

**Root cause:** AWS Amplify Hosting requires the `AWS Amplify` GitHub
App (`https://github.com/apps/aws-amplify-<region>`) to be installed on
the GitHub account before any new Amplify app's PAT-based connection
will validate. The error message is wildly misleading — it's the
generic "couldn't bootstrap the build container" and has nothing to do
with IAM. Watchtower was set up before this requirement (or was
installed at the time of its initial console-driven creation).

**Fix:**
1. Visit `https://github.com/apps/aws-amplify-us-east-2/installations/new`
2. Authorize the Amplify GitHub App for the repo
3. In the AWS Amplify console, click into the app, find "Update required" badge,
   click → "Reconnect Repository" → re-authorize via the GitHub App
4. After reconnect, builds work. The connection persists across `cdk
   deploy`s — this is a one-time per-app manual step that CDK can't do.

**For future agents:** if the Amplify build fails with the IAM-role
error and you've checked all the obvious things, **stop chasing IAM**.
Verify the GitHub App is installed AND the AWS console shows no
"Update required" banner on the app. CDK can create the app but
cannot complete the auth dance.

### Lesson 2: Amplify Hosting custom domains need us-east-1 certs

**Symptom:** first deploy of the Amplify app failed at the
`AWS::Amplify::Domain` step with
`Certificate settings are invalid. The custom certificate must exist
in the us-east-1 region.`

**Root cause:** Amplify Hosting fronts via CloudFront, which is
global / served from us-east-1 for cert lookup. The L2's
`addDomain({ customCertificate: cert })` requires that cert to be in
us-east-1, regardless of where Amplify itself runs.

**Fix:** Don't pass `customCertificate` at all. Let Amplify auto-issue
its own us-east-1 cert and emit a validation CNAME for Cloudflare.
That's what the current `disney-stack.ts` does (see comment at the
domain block). Trade-off: one extra DNS-validation CNAME at Cloudflare
on first deploy. Worth it vs maintaining a us-east-1 cert manually.

**Pre-issued us-east-2 cert is a sunk cost** — left it in ACM since
ACM certs are free. Can delete with:
```
aws acm delete-certificate \
  --certificate-arn arn:aws:acm:us-east-2:601669029997:certificate/86e2b187-f95c-4fc2-84be-77ee3b60ad34 \
  --region us-east-2 --profile watchtower
```

### Lesson 3: Turbopack mangles AWS SDK external module names

**Symptom:** Amplify build succeeds, deploy succeeds, home page renders,
but `/parks/[park]` returns bare `Internal Server Error` with no
visible logs (Amplify Hosting SSR Lambdas live in AWS-managed
infrastructure, no CloudWatch access from customer side).

**Diagnostic technique that finally worked:** wrote a temporary
`/api/debug/ddb` route handler that does a dynamic `import()` of the
AWS SDK inside a try/catch. That surfaced the actual error as JSON:
```
Failed to load external module @aws-sdk/client-dynamodb-2031539566c28ec5
Cannot find module '@aws-sdk/client-dynamodb-2031539566c28ec5'
```

**Root cause:** Next.js 16 + Turbopack + `serverExternalPackages`
listing the AWS SDK + pnpm's `.pnpm`-store layout. Turbopack treats
the SDK as external, emits a require for a hash-suffixed module name,
and pnpm's nested store doesn't expose it under that exact name.

**Fix:** drop the AWS SDK from `serverExternalPackages` so Turbopack
bundles it inline. ~600KB extra in the SSR chunk; meaningless at our
scale. Current `web/next.config.ts` is empty (no externals).

**For future agents:** if you see this error on any Amplify SSR
deployment using pnpm + Turbopack, this is the fix. The cost of
having the SDK external is zero (still bundles fine), the cost of
having it inline is a few hundred KB and zero runtime issues.

### Lesson 4: The L2 alpha auto-creates the compute role correctly; don't override

**Symptom:** When I provided a custom `computeRole` (named explicitly,
trust policy = `amplify.amazonaws.com`, identical to Watchtower's
auto-generated role), builds failed.

**Cause unclear** but consistent: the L2 alpha's auto-generated role
works; user-provided ones don't, even when functionally identical.
Possibly a CDK construct-tree path quirk that sets up some implicit
linkage we couldn't replicate.

**Fix:** don't pass `computeRole` to `new amplify.App(...)`. Let the
L2 create it. After the App is constructed, attach permissions via
`webApp.computeRole.addToPrincipalPolicy(...)` or
`dataTable.grantReadData(webApp.computeRole)`. Current
`disney-stack.ts` does this correctly.

### Lesson 5: Trust policy SourceArn conditions can break role assumption silently

**Symptom:** added `aws:SourceArn` condition to the compute role's
trust policy as a defense-in-depth measure (AWS recommends this for
service-principal trusts). Builds continued to fail.

**Cause:** Amplify's runtime assumption goes through a service-role
chain whose source ARN doesn't match the obvious branch ARN we
expected. The condition was too narrow.

**Fix:** keep the trust policy minimal — just `Service:
amplify.amazonaws.com` with no conditions. Watchtower's working role
has none, ours shouldn't either. Add SourceArn ONLY after careful
testing.

### Lesson 5 — round 2: trust-policy conditions can come back on deploy

**Symptom:** during M3 Phase 1 (2026-05-05), a `cdk deploy` that
modified the `AWS::Amplify::App` resource (added an env var + a
buildspec line) caused all subsequent Amplify builds to fail in <1s
with `Unable to assume specified IAM Role`. No "Update required"
banner in the console. GitHub App was installed and the connection
was healthy.

**Diagnosis:** the M2-B deploy used an older `@aws-cdk/aws-amplify-alpha`
that auto-generated the App's service role with `aws:SourceArn` +
`aws:SourceAccount` conditions on the trust policy (per AWS "best
practice"). That role was created and never re-templated until my M3
deploys — at which point Amplify started assuming the role through
its internal service-role chain, where the SourceArn condition no
longer matched. Builds failed silently.

Watchtower didn't have the issue because its role was created on an
even earlier alpha that emitted no conditions, and it hasn't been
re-templated since.

**Fix (manual, fast):**
```
aws iam update-assume-role-policy \
  --role-name DisneyStack-WebAppRole1AA0E641-GrEntXDsq4VT \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "amplify.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }' \
  --profile watchtower
```
Then re-trigger an Amplify build. Should succeed.

**Fix (permanent, in CDK):** `disney-stack.ts` now reaches into
`webApp.node.tryFindChild("Role")` and overrides the role's
`AssumeRolePolicyDocument` to the no-conditions form. The current
alpha generates the right thing already, so the override is a
defensive no-op today; if a future alpha upgrade re-introduces the
conditions, the next CDK deploy that touches the role will reset it.

**Diagnostic shortcut:** when an Amplify build fails with the IAM
error and no console banner is shown, compare the failing role's
trust policy against Watchtower's working one:
```
aws iam get-role --role-name DisneyStack-WebAppRole1AA0E641-... \
  --profile watchtower --query 'Role.AssumeRolePolicyDocument'
aws iam get-role --role-name WatchtowerStack-WebAppRole1AA0E641-... \
  --profile watchtower --query 'Role.AssumeRolePolicyDocument'
```
If MM has Conditions and Watchtower doesn't, this is the cause.

## Operational tasks

### Refresh AWS SSO (sessions last 8-12 hours)

```
aws sso login --profile watchtower
```

### Tail the poller

```
aws logs tail /aws/lambda/DisneyStack-PollerFunction3245213F-WGPWqKXc45P4 \
  --profile watchtower --region us-east-2 --follow
```

### Trigger a manual poll

```
aws lambda invoke --profile watchtower --region us-east-2 \
  --function-name DisneyStack-PollerFunction3245213F-WGPWqKXc45P4 \
  --cli-binary-format raw-in-base64-out --payload '{}' \
  /tmp/disney-poll.json && cat /tmp/disney-poll.json
```

### Trigger an Amplify build manually

```
aws amplify start-job --app-id d1ykat3qyev5c8 --branch-name main \
  --job-type RELEASE --region us-east-2 --profile watchtower
```

### Check latest build status

```
aws amplify list-jobs --app-id d1ykat3qyev5c8 --branch-name main \
  --region us-east-2 --profile watchtower --max-items 3 \
  --query 'jobSummaries[].{Job:jobId,Status:status,Started:startTime}' \
  --output table
```

### Read a failed Amplify build log

```
JOB=<jobId>
LOG_URL=$(aws amplify get-job --app-id d1ykat3qyev5c8 --branch-name main \
  --job-id $JOB --region us-east-2 --profile watchtower \
  --query 'job.steps[?stepName==`BUILD`].logUrl' --output text)
curl -s "$LOG_URL" | head -50
```

### Deploy CDK changes

```
cd infra
aws sso login --profile watchtower   # if expired
npx cdk diff --profile watchtower
npx cdk deploy --profile watchtower --require-approval never
```

CDK with `--require-approval never` is fine because we always run
`cdk diff` first. Don't skip the diff.

### Smoke-test the live URL

```
for path in / /parks/magic_kingdom /parks/epcot /parks/hollywood_studios \
            /parks/animal_kingdom /api/auth/providers; do
  curl -s -o /dev/null -w "  $path → %{http_code}\n" \
    -L https://magicmonitor.megillini.dev$path
done
```

All six should return 200.

### Read the Cognito-managed app-client secret (for local dev)

```
aws cognito-idp describe-user-pool-client \
  --user-pool-id us-east-2_ORhu761AY \
  --client-id 7m45buvekqqjt8c9vfmtbrfld4 \
  --region us-east-2 --profile watchtower \
  --query 'UserPoolClient.ClientSecret' --output text
```

Paste the result into `web/.env.local` as `COGNITO_CLIENT_SECRET`.
See `web/.env.local.example` for the full local-dev env shape.

## Known follow-ups (low priority)

These don't block anything; clean up when convenient.

1. **`infra/lib/disney-stack.ts`** — the `addToPrincipalPolicy` block
   adding logs perms to the SSR compute role has a misleading comment
   that says "without these, builds fail with 'Unable to assume
   specified IAM Role'." That's wrong; the actual cause was the GitHub
   connection state (Lesson 1). Either delete the block (Amplify
   auto-attaches what it needs) or update the comment to "matches
   Watchtower's auto-generated logs grant; harmless either way."

2. **`web/src/lib/dynamodb.ts`** — the `DISNEY_REGION` env-var
   fallback was added on the (incorrect) theory that the SSR runtime
   was Lambda@Edge in us-east-1. Confirmed the SSR is in us-east-2 and
   `AWS_REGION` is set correctly. The fallback is defensive but dead.

3. **us-east-2 ACM cert** — orphan cert issued during the false start.
   Free to keep, easy to delete (see Lesson 2).

4. **Watchtower** — check the AWS Amplify console for an "Update
   required" badge. If present, run the same Reconnect Repository flow
   we did for MM. Better to do it on your schedule than have a build
   silently break before a demo.

## What's next — M3 implementation plan

PROJECT.md describes M3 in narrative form. Implementation order:

### Phase 1: profile + park toggles (~half a day)

The smallest end-to-end slice that exercises auth + writes.

- `web/src/app/me/page.tsx` — gated by `auth()`, redirects to
  `/api/auth/signin/cognito` if no session
- Form fields: display name, Pushover user key
- Route handler `web/src/app/api/me/profile/route.ts` — POST writes
  `USER#<sub>/PROFILE` row with `name`, `pushover_user_key`,
  `updated_at`
- Park toggle UI: 4 checkboxes (Magic Kingdom, EPCOT, Hollywood
  Studios, Animal Kingdom). Each writes/deletes a
  `PARK#<key>/USER#<sub>` row on change.
- `disney-stack.ts`: broaden compute role grant from `grantReadData`
  to include scoped `UpdateItem`/`PutItem`/`DeleteItem`. Decision
  point — see "Decision to flag" below.

After this phase you can sign in, paste a Pushover key, toggle a
park, and the next 2-min poll will fire alerts to the right user
when a ride goes down.

### Phase 2: favorite-rides grid (~2-3 days)

The personalization that makes Pushover signal-to-noise sane.

- New page `/me/rides/[park]` with checkbox grid of all rides in
  that park
- Each checkbox toggles `USER#<sub>/FAV_RIDE#<ride_id>` row
- **Update poller logic** (`infra/lambda/poller/`): when a ride
  changes status, look up which users have favorited THAT ride AND
  have THAT park subscribed, fan out only to that intersection.
  Currently the poller fans out per-park. This is per-favorite ∩
  per-park-subscription.
- Probably needs a GSI on `FAV_RIDE#<ride_id>` to find favoriters
  efficiently. Or scan with filter — at this scale fine.

### Phase 3: new-user gating (~half a day)

Without this, fresh sign-ups silently get no alerts and don't know why.

- On first login (no `USER#<sub>/PROFILE` row), redirect to
  onboarding flow
- Onboarding: "Welcome → here are the parks → pick which rides
  matter to you" → save → land on dashboard
- Default zero rides = zero alerts (no surprise pings)

### Decision to flag for Phase 1

How strictly do you scope the SSR compute role's writes?

**Option A** (simpler): `UpdateItem` on the entire `DisneyData`
table; trust the route-handler code to enforce
`PK = USER#<auth().sub>`. Easier, faster to ship.

**Option B** (defense-in-depth): `dynamodb:LeadingKeys` IAM condition
to enforce per-user partition isolation at the IAM layer. Two layers
of protection. ~2 hours more CDK code.

For a portfolio app at this scale, A is fine. B is the "I can talk
about IAM least-privilege" interview answer. Personal use only? A.
Inviting non-trusted users? B.
