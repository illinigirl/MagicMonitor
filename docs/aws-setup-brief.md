# AWS setup brief — for an agent helping deploy a new app

Drop this into the system prompt or first message of a fresh agent
helping you ship a new app to AWS under the same account/conventions
as Watchtower and Magic Monitor. Self-contained — the agent shouldn't
need to ask basic questions about identity, region, or shared infra
after reading this.

---

## Quick reference

| | Value |
|---|---|
| AWS account | `601669029997` |
| Primary region | `us-east-2` |
| Secondary region | `us-east-1` (only for CloudFront/Amplify domain certs) |
| AWS CLI profile | `watchtower` (SSO via AWS IAM Identity Center) |
| Refresh SSO | `aws sso login --profile watchtower` (8-12h token TTL) |
| Root account email | `meganschott12@gmail.com` |
| GitHub org | `illinigirl` |
| Apex domain | `megillini.dev` (DNS managed in Cloudflare) |
| Cognito user pool | `us-east-2_ORhu761AY` (owned by Watchtower; reused) |
| Cognito hosted UI | `https://auth.megillini.dev` |
| Google federation | already set up on the shared Cognito pool |
| IaC | AWS CDK (TypeScript) |
| Frontend stack | Next.js 16 + Tailwind 4 + React 19 (App Router, Turbopack) |
| Package manager | pnpm (web/) and npm (infra/) — yes, mixed |
| Hosting | AWS Amplify Hosting (SSR Next.js apps with custom domain) |
| Backend runtime | Python 3.12 (Lambdas — default), Node.js 22 (Amplify SSR) |
| Storage | DynamoDB single-table per project |
| Notifications | Pushover (one App Token per project, in SSM) |

---

## Sibling projects (already in the AWS account)

The new app should fit the family pattern these set up.

1. **Watchtower** — the original. Owns the shared Cognito user pool
   `us-east-2_ORhu761AY` (with Google IdP) and the GitHub OIDC
   provider. Sets up the auth.megillini.dev hosted UI.
2. **Magic Monitor** (`/Users/meganschott/Documents/Pi/Disney`) — the
   sibling. Imports the Watchtower Cognito pool via a SECOND app
   client (NOT a new pool). Owns its own DDB table (`DisneyData`),
   Lambda poller, Amplify Hosting app at
   `magicmonitor.megillini.dev`, and a Pushover App Token in SSM
   (`/disney/pushover/app_token`).

**Pattern for the new project:** be a third sibling. Reuse the
Cognito pool via a third app client. Get its own subdomain off
`megillini.dev`. Own its own DDB table + stack + SSM params +
GitHub OIDC role.

---

## What to reuse (vs. set up fresh)

### Reuse from existing infrastructure
- **Cognito user pool** `us-east-2_ORhu761AY`. Add a new app client
  with the new domain's callback URLs. Do NOT create another pool.
- **Cognito hosted UI** at `auth.megillini.dev`. Already supports
  Google sign-in; adding the new app's callback URLs to the existing
  hosted UI config gets sign-in working with minimal work.
- **GitHub OIDC provider** in IAM. Already exists. The new project
  creates its own OIDC-trusting role; the provider it trusts is
  the existing one.
- **AWS Amplify GitHub App** must be installed on the GitHub
  account BEFORE Amplify Hosting will validate a new app's repo
  connection. It may already be installed from prior projects, but
  per RUNBOOK Lesson 1 a NEW Amplify app sometimes still needs a
  "Reconnect Repository" / "Update required" click after the CDK
  creates the app. If the first build fails with
  `Unable to assume specified IAM Role` and you've checked
  everything else, stop chasing IAM — go look for the
  "Update required" banner in the AWS Amplify console.

### Set up fresh per new project
- New CloudFormation stack (e.g., `MyNewAppStack`)
- New CDK app in TypeScript (mirror the layout of `infra/` in
  Magic Monitor: `infra/bin/<app>.ts`, `infra/lib/<app>-stack.ts`,
  `infra/lambda/` for Python Lambdas)
- New DynamoDB single table (pattern: PK + SK strings, no GSI
  until access patterns demand it)
- New Cognito app client on the existing pool
- New Amplify Hosting app + custom subdomain off megillini.dev
- New GitHub repo under `illinigirl`
- New GitHub OIDC deploy role (mirror
  `arn:aws:iam::601669029997:role/MagicMonitorGithubDeploy`'s
  trust policy — trusts the GitHub OIDC provider, scoped to the
  new repo)
- New SSM params for any secrets (one path per project, e.g.,
  `/myapp/pushover/app_token`)
- New Cloudflare DNS records (CNAME from the new subdomain to the
  Amplify-managed cert validation target)

---

## Hard-won AWS lessons (don't re-learn these)

All seven of these cost real hours. The first five came from
Magic Monitor's M2-B deploy; #6 and #7 came from setting up the
Megan Builds blog. Full debug logs for #1-5 in
`/Users/meganschott/Documents/Pi/Disney/RUNBOOK.md` under
"M2-B journey."

1. **Amplify Hosting needs the GitHub App installed.** New Amplify
   apps fail to assume their build role until the
   `AWS Amplify` GitHub App at
   `github.com/apps/aws-amplify-us-east-2` is authorized for the
   repo. After CDK creates the Amplify app, the AWS console shows
   an "Update required" badge. Click into it → Reconnect Repository
   → re-authorize via the GitHub App. One-time manual step CDK
   can't do.

2. **Amplify custom domains need us-east-1 certs.** Amplify Hosting
   fronts via CloudFront, which looks up certs from us-east-1
   regardless of where the app runs. **Solution: don't pass
   `customCertificate` to the L2 at all.** Let Amplify auto-issue
   its cert and emit a Cloudflare validation CNAME. One extra DNS
   record at Cloudflare on first deploy; worth it vs. maintaining
   a us-east-1 cert.

3. **Don't add the AWS SDK to `serverExternalPackages`** in
   Next.js 16 + Turbopack + pnpm. Turbopack emits a require for a
   hash-suffixed module name and pnpm's nested store doesn't
   expose it. Symptom: `/some/route` returns bare 500s with no
   CloudWatch trail. Fix: drop the SDK from externals. ~600KB
   extra in the SSR chunk, meaningless at this scale.

4. **Don't pass a custom `computeRole` to `amplify.App`.** The L2
   alpha auto-generates the right role; user-provided roles fail
   even when functionally identical. After construction, attach
   extra permissions via
   `webApp.computeRole.addToPrincipalPolicy(...)`.

5. **Defensively override the Amplify service role's
   `AssumeRolePolicyDocument`.** Earlier versions of
   `@aws-cdk/aws-amplify-alpha` generated a service role with
   `aws:SourceArn` + `aws:SourceAccount` conditions, which Amplify
   silently fails to assume on later cdk deploys that re-template
   the role. In CDK:
   ```typescript
   const appRole = webApp.node.tryFindChild("Role") as iam.CfnRole;
   appRole.assumeRolePolicyDocument = {
     Version: "2012-10-17",
     Statement: [{
       Effect: "Allow",
       Principal: { Service: "amplify.amazonaws.com" },
       Action: "sts:AssumeRole",
     }],
   };
   ```
   Defensive no-op today if the current alpha doesn't generate
   conditions; reapplies if a future alpha re-introduces them.

6. **`@aws-cdk/aws-amplify-alpha` with `platform: WEB` (static)
   silently fails to assume its IAM role.** Every build errors
   with "Unable to assume specified IAM Role" regardless of role
   config or trust-policy overrides. The same module with
   `platform: WEB_COMPUTE` (SSR) works fine on the same trust
   policy. Reproduced on `2.251.0-alpha.0`. Spent a full
   afternoon ruling out IAM before discovering it's an alpha bug
   specific to the static platform.
   **Workaround:** provision `Amplify::App` + `Branch` + `Domain`
   through the AWS Console instead of CDK. Keep CDK for the
   IAM/secrets layer only (GitHub OIDC deploy role, SSM params).
   Drop an `amplify.yml` at the repo root so the Console reads
   the buildSpec from source — that part still works the way
   you'd expect.

7. **A repeatedly delete/recreated Amplify custom domain leaves
   a stale CloudFront alias claim that takes hours to clear —
   sometimes longer than you have.** Symptom: domain association
   status flips to `FAILED` with "incorrectly configured DNS
   record points to another CloudFront distribution," even though
   DNS resolves correctly. Waiting 45+ minutes does not clear it.
   The claim is internal to CloudFront's alias registry; you
   can't see or release it from the console.
   **Diagnostic:** try a fresh subdomain (one with no prior
   association history). If that succeeds, the failure was the
   stale claim, not your DNS or cert config.
   **Decision-time tradeoff:** wait a day-plus for the original
   subdomain to clear, or pivot to a different subdomain
   permanently. Megan Builds picked the latter
   (`blog.megillini.dev` → `meganbuilds.megillini.dev`) and the
   pivot was actually a brand upgrade — worth knowing the option
   exists when the wait would block a deploy day.

---

## Python version

**Default to Python 3.12** for any new Lambda. The rule that matters:
the local venv version should match the Lambda runtime, so
version-specific syntax / library wheels show up as failures
locally before they hit production.

- Matches Magic Monitor (already running 3.12 in Lambda) — share
  troubleshooting habits, library wheels, mental model.
- AWS supports 3.12 as a fully-supported Lambda runtime through
  ~2029, no deprecation pressure.
- Widest library wheel coverage (3.13 wheels are catching up but
  some C-extension libs still ship 3.12 first).

Set up:
```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

In CDK:
```typescript
runtime: lambda.Runtime.PYTHON_3_12,
```

Pick 3.13 only if a required library specifically needs it (rare
in 2026) or you're writing a local-only utility where Lambda
parity doesn't matter (the Magic Monitor MCP server is an example
— it runs locally as a subprocess of Claude Desktop, not in
Lambda, so its venv is 3.13).

## CDK / deploy conventions

- One stack per project, named `<ProjectName>Stack`.
- Always run `npx cdk diff --profile watchtower` before
  `npx cdk deploy --profile watchtower`. The diff is the safety
  net.
- `--require-approval never` is fine because we always diff first.
  Don't skip the diff.
- For destructive AWS ops (drop tables, force-recreate, delete
  Lambdas, etc.), confirm with Megan before executing.
- Lambda code lives in `infra/lambda/<function-name>/` with its
  own `requirements.txt` (Python) or bundled handler (Node). CDK's
  `PythonFunction` construct bundles + uploads on deploy.
- SSM params follow `/<project>/<service>/<key>` (e.g.,
  `/disney/pushover/app_token`). Use `SecureString`.

---

## Operational commands the agent will likely need

> **SSO refresh caveat.** `aws sso login` opens a browser to confirm
> the device code. Claude Code's `!` shell-exec prefix runs in a
> non-interactive subprocess and won't actually launch the browser,
> so the refresh silently fails and every subsequent AWS call still
> returns "Token has expired." Run `aws sso login --profile watchtower`
> in a real terminal (Terminal.app, iTerm, etc.), confirm in the
> browser, then return to the agent session.

> **Git push from the agent shell.** SSH-based pushes fail because
> the agent's shell doesn't have the user's SSH keys loaded — you'll
> see `Permission denied (publickey)`. **Do NOT switch the remote to
> HTTPS permanently** (that mutates user config). Instead, push
> one-shot via HTTPS inline:
> ```bash
> git push https://github.com/illinigirl/<repo>.git <branch>
> ```
> This works because `gh` CLI is logged in (`gh auth status` ✓) and
> set as the git credential helper for github.com (verify with
> `git config --get-all credential.https://github.com.helper` →
> should include `!/opt/homebrew/bin/gh auth git-credential`). The
> token is stored in the macOS keychain; the helper supplies it
> automatically for HTTPS pushes.
>
> If `gh auth status` shows logged out, that's a user-level fix —
> ask Megan to run `gh auth login` in a real terminal. Don't try to
> work around it with `git config` mutations.
>
> Repos that are already HTTPS (e.g., Magic Monitor's
> `MagicMonitor.git`) push normally with `git push origin <branch>`.
> Repos using SSH remotes (e.g., `megan-builds`) need the HTTPS-URL-
> inline form above per push.

```bash
# Refresh SSO (every 8-12h) — must run in a real terminal, not via `!`
aws sso login --profile watchtower

# Smoke-test live URL
for path in / /api/auth/providers; do
  curl -s -o /dev/null -w "  $path → %{http_code}\n" \
    -L https://<your-subdomain>.megillini.dev$path
done

# Tail Lambda logs
aws logs tail /aws/lambda/<FunctionName> \
  --profile watchtower --region us-east-2 --follow

# Manual invoke
aws lambda invoke --profile watchtower --region us-east-2 \
  --function-name <FunctionName> \
  --cli-binary-format raw-in-base64-out --payload '{}' \
  /tmp/invoke.json && cat /tmp/invoke.json

# Trigger Amplify build manually
aws amplify start-job --app-id <amplify-app-id> --branch-name main \
  --job-type RELEASE --region us-east-2 --profile watchtower

# Read failed Amplify build log
JOB=<jobId>
LOG_URL=$(aws amplify get-job --app-id <amplify-app-id> --branch-name main \
  --job-id $JOB --region us-east-2 --profile watchtower \
  --query 'job.steps[?stepName==`BUILD`].logUrl' --output text)
curl -s "$LOG_URL" | head -50
```

---

## Megan's working preferences (apply to all interactions)

- **She architects; the agent writes the code.** Don't ask her to
  write code herself; she'll set the design direction and approve.
- **Options with tradeoffs, not single paths.** When there's a
  decision to make, present 2-3 options with pros/cons and a
  recommendation.
- **Push back on scope creep.** She trusts implementer judgment but
  wants the boundary held — if she's about to over-build, say so.
- **Clean, well-commented code.** Comments explain the WHY (hidden
  constraints, workarounds, surprises) — not the WHAT.
- **Sessions span multiple days.** Don't assume "today" — check
  the system date before saying "today did X."
- **She catches data-quality issues sharply.** When she points
  one out, investigate concretely rather than deflect or hand-wave.
- **For frontend changes**, start the dev server and test in a
  browser — not just type-check.
- **For destructive AWS ops** (delete, force-recreate, etc.),
  confirm before executing.
- **Default to her actual style instead of asking the same
  question every session.** When her workflow has a clear pattern
  (e.g., LL grace buffers in the planner, always-show-options
  for decisions), apply it by default with one upfront mention.

---

## Where to look for context

- `~/Documents/Pi/<project>/` — each sibling project's working dir
- `~/Documents/Pi/Disney/PROJECT.md` — Magic Monitor roadmap +
  done section; good reference for how Megan structures milestones
- `~/Documents/Pi/Disney/RUNBOOK.md` — operational layer for MM;
  the M2-B Lesson section is required reading before touching
  Amplify Hosting in CDK
- `~/Documents/Pi/Disney/README.md` — portfolio-grade architecture
  doc; good reference for tone + structure
- `~/Documents/Pi/Disney/infra/lib/disney-stack.ts` — canonical
  example of the multi-resource stack pattern (DDB + Lambda +
  EventBridge + Amplify + Cognito app client + GitHub OIDC role)

---

## What to ASK Megan upfront for the new project

Before writing CDK or code, get answers to these from her:

1. **What's the new app's name + subdomain?** (e.g.,
   `coolthing.megillini.dev`) — drives stack name, repo name,
   SSM path prefix.
2. **Does it need authenticated users?** If yes, you'll add a
   Cognito app client; if no, simpler stack.
3. **Does it need a scheduled Lambda?** (EventBridge cron — the
   Magic Monitor poller pattern.)
4. **Does it need notifications?** If yes, set up a new Pushover
   App Token (she'll register it manually at pushover.net/apps/build)
   and store the token in SSM.
5. **Frontend or backend-only?** Frontend → Amplify Hosting + the
   Next.js 16 setup. Backend-only → just Lambda + API Gateway or
   direct invocations.
6. **What's it for?** Drives the data model + decisions about
   single-table DDB shape.
