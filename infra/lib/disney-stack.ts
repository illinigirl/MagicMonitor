import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as codebuild from "aws-cdk-lib/aws-codebuild";
import * as amplify from "@aws-cdk/aws-amplify-alpha";
import * as path from "path";
import { execSync } from "child_process";
import * as fs from "fs";

/** SSM parameter names for the Pushover credentials. Bootstrapped manually
 * (one-time `aws ssm put-parameter`) so secrets never live in CDK,
 * CloudFormation, or git. Same hygiene pattern as the earlier project's TMDB key. */
const PUSHOVER_APP_TOKEN_PARAM = "/disney/pushover/app_token";
const PUSHOVER_USER_KEY_PARAM = "/disney/pushover/megan_user_key";

/** Park entity IDs from themeparks.wiki — duplicated here from the Lambda
 * code so they're visible in the CDK config (and we could later wire them
 * into per-park Lambda env vars if we ever split polling per park). */
const PARK_KEYS = [
  "magic_kingdom",
  "epcot",
  "hollywood_studios",
  "animal_kingdom",
];

// ─── M2-B: auth + production deploy constants ───────────────────────

/** Public hostname for the dashboard. Cloudflare CNAME points here. */
const APP_DOMAIN = "magicmonitor.megillini.dev";

// Note: there is NO ACM cert constant here, deliberately. Amplify
// custom domains require a us-east-1 cert (Amplify Hosting fronts
// via CloudFront, which is global/us-east-1 for cert lookup). Rather
// than maintain a us-east-1 cert ourselves and pass it via the L2
// `customCertificate` prop, we let `addDomain` auto-issue and manage
// its own cert — same approach an earlier project uses. The trade-off is a
// second validation CNAME at Cloudflare (Amplify emits it at deploy
// time and the deploy stalls until the cert validates), in exchange
// for one less moving part in our IaC.

/** Secrets Manager name for the GitHub PAT used by Amplify to pull
 * source from illinigirl/MagicMonitor. Separate from the earlier project's PAT
 * secret so rotating one doesn't impact the other. */
const GITHUB_TOKEN_SECRET = "/magicmonitor/github-token";

/** Secrets Manager name for the NextAuth/Auth.js session-encryption
 * secret. 32 random bytes, bootstrapped manually 2026-05-05. Rotates
 * by re-creating the secret + redeploying Amplify (existing sessions
 * invalidate, which is fine). */
const NEXTAUTH_SECRET_NAME = "/magicmonitor/nextauth-secret";

/** Cognito user pool that owns Magic Monitor's auth. Owned by
 * an earlier project — imported here read-only as `IUserPool`. The pool
 * also owns the Google IdP and the `auth.megillini.dev` custom hosted-UI
 * domain, both of which Magic Monitor reuses verbatim. Cross-stack
 * coupling: if an earlier project destroys this pool, MM auth breaks too. */
const IMPORTED_USER_POOL_ID = "us-east-2_ORhu761AY";

/** Cognito hosted-UI base URL — owned by an earlier project at
 * auth.megillini.dev. Reused as-is so MM doesn't need its own auth
 * subdomain or its own us-east-1 cert (Cognito custom domains require
 * us-east-1 certs regardless of pool region; an earlier project already paid
 * that cost). The Google OAuth callback configured in Google Cloud
 * Console points at https://auth.megillini.dev/oauth2/idpresponse,
 * which works for any app client on the pool — no Google Cloud
 * changes needed for MM. */
const COGNITO_DOMAIN_URL = "https://auth.megillini.dev";

/** GitHub OIDC provider ARN (pre-existing — an earlier project created it).
 * AWS only allows one provider per token URL per account, so MM imports
 * the existing one rather than creating a new one. */
const GITHUB_OIDC_PROVIDER_ARN =
  "arn:aws:iam::601669029997:oidc-provider/token.actions.githubusercontent.com";

/** GitHub repo whose pushes Amplify auto-builds and whose Actions
 * assume the deploy role via OIDC. */
const GITHUB_OWNER = "illinigirl";
const GITHUB_REPO = "MagicMonitor";

/**
 * Local Python bundling for the poller Lambda. Same approach as
 * an earlier project — uses host python3 to install deps with manylinux wheels,
 * falls back to Docker if python3 isn't available. Cross-compiles from
 * macOS to Lambda's Linux x86_64 by forcing the platform/wheel flags.
 */
function bundleLambdaAsset(assetPath: string): lambda.AssetCode {
  return lambda.Code.fromAsset(assetPath, {
    bundling: {
      image: lambda.Runtime.PYTHON_3_12.bundlingImage,
      command: [
        "bash",
        "-c",
        [
          "pip install --no-cache-dir -r requirements.txt -t /asset-output",
          // Lambda runtime ships boto3/botocore — strip the bundled
          // copies to keep the asset small and avoid version skew.
          "rm -rf /asset-output/boto3 /asset-output/botocore /asset-output/boto3-*.dist-info /asset-output/botocore-*.dist-info",
          "cp -au . /asset-output",
        ].join(" && "),
      ],
      local: {
        tryBundle(outputDir: string): boolean {
          try {
            execSync("python3 --version", { stdio: "ignore" });
          } catch {
            return false;
          }
          execSync(
            [
              "python3 -m pip install --no-cache-dir",
              "--platform manylinux2014_x86_64",
              "--implementation cp",
              "--python-version 3.12",
              "--only-binary=:all:",
              "--upgrade",
              `--target ${outputDir}`,
              "-r requirements.txt",
            ].join(" "),
            { cwd: assetPath, stdio: "inherit" },
          );
          for (const pkg of ["boto3", "botocore"]) {
            execSync(
              `rm -rf "${outputDir}/${pkg}" "${outputDir}/${pkg}"-*.dist-info`,
              { stdio: "inherit" },
            );
          }
          const skip = new Set([
            "requirements.txt",
            ".venv",
            "tests",
            "pytest.ini",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
          ]);
          for (const entry of fs.readdirSync(assetPath)) {
            if (skip.has(entry)) continue;
            const src = path.join(assetPath, entry);
            const dst = path.join(outputDir, entry);
            execSync(`cp -R "${src}" "${dst}"`, { stdio: "inherit" });
          }
          return true;
        },
      },
    },
  });
}

/**
 * Disney stack — phase 1: poll themeparks.wiki on a schedule, diff
 * against DynamoDB, fire Pushover alerts when ride status changes.
 *
 * Architecture:
 *   EventBridge Schedule (every 2 min)
 *       │
 *       ▼
 *   Poller Lambda (Python)
 *       │
 *       ├── reads/writes DynamoDB (rides + history + subscriptions)
 *       ├── reads SSM (Pushover credentials)
 *       └── posts to api.pushover.net for each subscriber × event
 *
 * Multi-user-ready schema (M2 will add Cognito + per-user UI without
 * a migration):
 *   PK / SK
 *   RIDE#<id>          / STATE                  — current ride state
 *   RIDE#<id>          / HIST#<iso_ts>          — change history (90d TTL)
 *   RIDE#<id>          / DOWN_SINCE             — track down duration
 *   RIDE#<id>          / COOLDOWN#DOWN          — alert dedup (15m TTL)
 *   USER#<id>          / PROFILE                — name, pushover_user_key
 *   PARK#<key>         / USER#<id>              — subscription (fanout)
 */
export class DisneyStack extends cdk.Stack {
  public readonly tableName: string;
  public readonly pollerFunctionName: string;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ─── Data: single DynamoDB table ────────────────────────────────
    // PAY_PER_REQUEST keeps idle cost at zero. TTL on the `ttl`
    // attribute auto-expires history rows and alert cooldowns.
    const dataTable = new dynamodb.Table(this, "DataTable", {
      tableName: "DisneyData",
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "ttl",
      // RETAIN: ride history + user data should survive a stack
      // rebuild. Cheap to keep; expensive to rebuild months of polls.
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
    });

    // GSI: park_key + SK.
    //
    // Replaces the paginated Scan + FilterExpression that powers
    // `getParkRides()` in the web reader. STATE rows carry park_key
    // already; Query against this index returns only the matching
    // park's rows in one round-trip (~25 items vs walking the whole
    // table). Drops per-park-page-load cost from ~$0.03 to ~$0.0001
    // and removes the implicit "table fits in one Scan page"
    // assumption that caused the 2026-05-24 silent regression — the
    // category-level fix, not just the immediate pagination patch.
    //
    // WAIT# and HIST# rows also carry park_key and get indexed too.
    // That adds ~$1.25/mo of GSI storage (the index is roughly the
    // same size as the table) and ~doubles write costs across all
    // writes (still under $1/mo at current pace), but it also opens
    // up future park-scoped Queries on WAIT#/HIST# if useful (e.g.,
    // "average wait by hour for Magic Kingdom" without walking
    // every ride individually).
    //
    // Sort key is `SK` so callers can use `SK = "STATE"` for the
    // STATE-rows-only case, or `SK begins_with "WAIT#"` for the
    // observations case, etc. AWS backfills existing rows
    // automatically when the GSI is created — no schema migration
    // code needed.
    dataTable.addGlobalSecondaryIndex({
      indexName: "park_key-SK-index",
      partitionKey: { name: "park_key", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.tableName = dataTable.tableName;

    // ─── Compute: poller Lambda ─────────────────────────────────────
    const pollerFn = new lambda.Function(this, "PollerFunction", {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.X86_64,
      handler: "index.handler",
      code: bundleLambdaAsset(path.join(__dirname, "../lambda/poller")),
      // 512MB: balances cold-start vs cost. Polling 4 parks is mostly
      // I/O wait, not CPU. Bump to 1024 if cold-start latency matters.
      memorySize: 512,
      // 60s: each park is one HTTPS call (~1-2s), then DynamoDB
      // batch writes + per-event Pushover sends. With 4 parks × ~30
      // attractions, even worst case is well under 30s.
      timeout: cdk.Duration.seconds(60),
      tracing: lambda.Tracing.ACTIVE,
      environment: {
        DISNEY_TABLE_NAME: dataTable.tableName,
        PUSHOVER_APP_TOKEN_PARAM,
        PUSHOVER_USER_KEY_PARAM,
        PARK_KEYS: PARK_KEYS.join(","),
        // How long a ride must be down before second alert fires.
        SECOND_ALERT_MINS: "45",
        // Cooldown between repeat DOWN alerts for the same ride.
        DOWN_ALERT_COOLDOWN_SECS: "900",
        // Days of status history to retain (TTL on HIST# items).
        // Bumped 90 → 1825 (5 yr) for M6-B Phase 4: the aggregator
        // reconstructs ride downtime by walking HIST# transitions,
        // so it needs years of history, not weeks. Backfill script
        // (tools/backfill-pi-to-ddb.py --mode hist) stamps the same
        // TTL on imported rows.
        HISTORY_RETENTION_DAYS: "1825",
      },
      // No reserved concurrency — account-wide cap is 10 and
      // an earlier project already uses ~3-4 concurrent slots. The poller
      // runs alone every 2 min so concurrency is effectively 1.
    });

    this.pollerFunctionName = pollerFn.functionName;

    dataTable.grantReadWriteData(pollerFn);

    // SSM read for Pushover credentials. Tightly scoped to the two
    // parameter ARNs — no wildcards. SecureString decryption is
    // granted via the AWS-managed KMS alias for SSM (no explicit KMS
    // permission needed when using the default key).
    pollerFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter", "ssm:GetParameters"],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter${PUSHOVER_APP_TOKEN_PARAM}`,
          `arn:aws:ssm:${this.region}:${this.account}:parameter${PUSHOVER_USER_KEY_PARAM}`,
        ],
      }),
    );

    // ─── Schedule: EventBridge every 2 minutes ──────────────────────
    // 2-minute cadence matches the Pi version. themeparks.wiki rate
    // limits aren't published but 4 calls every 2 min × 24h × 30d =
    // ~85k requests/month, well under typical free-tier ceilings.
    //
    // Could narrow this to "park hours only" later (saves ~50% of
    // invocations) but Lambda free tier covers it either way.
    new events.Rule(this, "PollerSchedule", {
      ruleName: "disney-poller-every-2min",
      description: "Trigger Disney poller Lambda every 2 minutes",
      schedule: events.Schedule.rate(cdk.Duration.minutes(2)),
      targets: [new targets.LambdaFunction(pollerFn)],
    });

    // ─── Outputs (M1) ────────────────────────────────────────────────
    new cdk.CfnOutput(this, "TableName", {
      value: dataTable.tableName,
      description: "DynamoDB single table for ride state + subscriptions",
    });
    new cdk.CfnOutput(this, "PollerFunctionName", {
      value: pollerFn.functionName,
      description: "Poller Lambda — invoke manually with `aws lambda invoke` to test",
    });
    new cdk.CfnOutput(this, "PollerLogGroup", {
      value: `/aws/lambda/${pollerFn.functionName}`,
      description: "CloudWatch log group — `aws logs tail` to watch live",
    });

    // ═════════════════════════════════════════════════════════════════
    // M2-B: auth + production deploy
    //
    // Architecture:
    //   GitHub push to main
    //       │
    //       ▼
    //   Amplify build (pnpm install + next build)
    //       │
    //       ▼
    //   Amplify SSR Lambda  ◄─── browser hits magicmonitor.megillini.dev
    //       │  (read DynamoDB directly via ssrComputeRole — no APIGW)
    //       ▼
    //   DynamoDB DataTable (RIDE#*/STATE rows)
    //
    //   Auth: NextAuth → Cognito hosted UI (auth.megillini.dev, owned
    //   by an earlier project) → Google. MM has its own app client on
    //   the shared user pool. M3 will grow this Lambda's role to
    //   include scoped writes for per-user toggles + favorites.
    // ═════════════════════════════════════════════════════════════════

    // ─── Cognito: 2nd app client on the earlier project's existing user pool ──
    // Imported (read-only handle). The pool itself, the Google IdP,
    // and the auth.megillini.dev custom domain were all created in
    // an earlier project — Magic Monitor's only Cognito-side resource
    // is this app client.
    const userPool = cognito.UserPool.fromUserPoolId(
      this,
      "ImportedUserPool",
      IMPORTED_USER_POOL_ID,
    );

    // GOOGLE here is a static string-name reference ("Google") that
    // resolves at runtime against the IdP attached to the imported
    // pool. We can't add a CDK dependency edge across stacks, so the
    // contract is "an earlier project owns the Google IdP, MM consumes it."
    // If that earlier project's IdP is ever removed, MM sign-ins break too.
    const userPoolClient = new cognito.UserPoolClient(this, "UserPoolClient", {
      userPool,
      userPoolClientName: "magicmonitor-web",
      // PKCE is conceptually sufficient, but NextAuth v5's Cognito
      // provider validation requires a clientSecret in its config —
      // the secret stays server-side in the SSR Lambda env and never
      // reaches the browser. Same constraint an earlier project hit.
      generateSecret: true,
      supportedIdentityProviders: [
        cognito.UserPoolClientIdentityProvider.GOOGLE,
      ],
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
        ],
        callbackUrls: [
          `https://${APP_DOMAIN}/api/auth/callback/cognito`,
          // Local dev (`pnpm dev` → :3000)
          "http://localhost:3000/api/auth/callback/cognito",
        ],
        logoutUrls: [
          `https://${APP_DOMAIN}/`,
          "http://localhost:3000/",
        ],
      },
      refreshTokenValidity: cdk.Duration.days(30),
      accessTokenValidity: cdk.Duration.hours(1),
      idTokenValidity: cdk.Duration.hours(1),
      // Don't leak whether a Cognito user exists. Default post-2024.
      preventUserExistenceErrors: true,
    });

    const cognitoIssuer = `https://cognito-idp.${this.region}.amazonaws.com/${IMPORTED_USER_POOL_ID}`;

    // ─── Amplify app ─────────────────────────────────────────────────
    // Same monorepo build pattern as an earlier project (pnpm + .env.production
    // materialization). The L2 alpha sets the legacy `OauthToken` field
    // for GitHub PATs; we override to `AccessToken` (the modern field)
    // via the L1 escape hatch below.
    const githubToken = secretsmanager.Secret.fromSecretNameV2(
      this,
      "GithubTokenSecret",
      GITHUB_TOKEN_SECRET,
    );

    const webApp = new amplify.App(this, "WebApp", {
      appName: "magicmonitor",
      sourceCodeProvider: new amplify.GitHubSourceCodeProvider({
        owner: GITHUB_OWNER,
        repository: GITHUB_REPO,
        oauthToken: githubToken.secretValue,
      }),
      // WEB_COMPUTE = SSR. The parks page (`app/parks/[park]/page.tsx`)
      // is a dynamic Server Component that scans DDB on each request.
      // Compute role is auto-created by the L2 (we attach the DDB
      // grant after the App is constructed — see below). An earlier
      // attempt to provide an explicit pre-built role failed builds
      // with "Unable to assume specified IAM Role"; the L2's internal
      // role wiring carries some implicit configuration that the
      // public API doesn't expose, so deferring to it is the
      // path-of-least-surprise.
      platform: amplify.Platform.WEB_COMPUTE,
      environmentVariables: {
        // Read at build time and materialized into .env.production by
        // the build spec below; consumed at SSR runtime by Next.js.
        DISNEY_TABLE_NAME: dataTable.tableName,
        // M3: SSR reads the Pushover app token from SSM at runtime
        // (via the SSM grant on computeRole below) to validate
        // user-supplied user keys before saving them. Only the
        // PARAMETER NAME is in env — the secret value never leaves
        // SSM. Rotates without a redeploy.
        PUSHOVER_APP_TOKEN_PARAM,
        AMPLIFY_MONOREPO_APP_ROOT: "web",
        AMPLIFY_DIFF_DEPLOY: "false",
        // Avoid the "do you accept telemetry?" interactive prompt
        // on the build runner.
        NEXT_TELEMETRY_DISABLED: "1",
        // ─── NextAuth + Cognito ──────────────────────────────────
        // AUTH_TRUST_HOST tells Auth.js to trust the X-Forwarded-Host
        // header (Amplify is behind CloudFront).
        AUTH_TRUST_HOST: "true",
        NEXTAUTH_URL: `https://${APP_DOMAIN}`,
        // unsafeUnwrap returns a CFN dynamic-reference string
        // ("{{resolve:secretsmanager:…}}"), not the literal value —
        // CFN unwinds it at deploy time, so the actual secret never
        // lands in source, cdk.out, or the synthesized template.
        NEXTAUTH_SECRET: cdk.SecretValue.secretsManager(
          NEXTAUTH_SECRET_NAME,
        ).unsafeUnwrap(),
        COGNITO_ISSUER: cognitoIssuer,
        COGNITO_CLIENT_ID: userPoolClient.userPoolClientId,
        // Same dynamic-reference treatment — CDK fetches the
        // Cognito-managed client secret at deploy time via an
        // AwsCustomResource under the hood.
        COGNITO_CLIENT_SECRET: userPoolClient.userPoolClientSecret.unsafeUnwrap(),
        COGNITO_DOMAIN_URL,
        NEXT_PUBLIC_COGNITO_DOMAIN_URL: COGNITO_DOMAIN_URL,
        NEXT_PUBLIC_COGNITO_CLIENT_ID: userPoolClient.userPoolClientId,
      },
      buildSpec: codebuild.BuildSpec.fromObjectToYaml({
        version: "1.0",
        applications: [
          {
            appRoot: "web",
            frontend: {
              phases: {
                preBuild: {
                  commands: [
                    "corepack enable",
                    "corepack prepare pnpm@latest --activate",
                    "pnpm install --frozen-lockfile",
                  ],
                },
                build: {
                  commands: [
                    // Amplify only injects env vars at build time —
                    // anything not prefixed NEXT_PUBLIC_ is gone at
                    // SSR runtime. Materializing here so Next.js
                    // reads them out of .env.production at request
                    // time. AUTH_* are Auth.js v5 canonical names;
                    // NEXTAUTH_* are kept as v4-compat fallbacks.
                    "echo \"DISNEY_TABLE_NAME=$DISNEY_TABLE_NAME\" >> .env.production",
                    "echo \"PUSHOVER_APP_TOKEN_PARAM=$PUSHOVER_APP_TOKEN_PARAM\" >> .env.production",
                    "echo \"AUTH_URL=$NEXTAUTH_URL\" >> .env.production",
                    "echo \"AUTH_SECRET=$NEXTAUTH_SECRET\" >> .env.production",
                    "echo \"NEXTAUTH_URL=$NEXTAUTH_URL\" >> .env.production",
                    "echo \"NEXTAUTH_SECRET=$NEXTAUTH_SECRET\" >> .env.production",
                    "echo \"AUTH_TRUST_HOST=$AUTH_TRUST_HOST\" >> .env.production",
                    "echo \"COGNITO_ISSUER=$COGNITO_ISSUER\" >> .env.production",
                    "echo \"COGNITO_CLIENT_ID=$COGNITO_CLIENT_ID\" >> .env.production",
                    "echo \"COGNITO_CLIENT_SECRET=$COGNITO_CLIENT_SECRET\" >> .env.production",
                    "echo \"COGNITO_DOMAIN_URL=$COGNITO_DOMAIN_URL\" >> .env.production",
                    "pnpm build",
                  ],
                },
              },
              artifacts: {
                baseDirectory: ".next",
                files: ["**/*"],
              },
              cache: {
                paths: ["node_modules/**/*", ".next/cache/**/*"],
              },
            },
          },
        ],
      }),
    });

    // Escape hatch: swap OauthToken (legacy field set by alpha module)
    // for AccessToken (modern field that fine-grained PATs need).
    // Same workaround an earlier project uses.
    const cfnApp = webApp.node.defaultChild as cdk.CfnResource;
    cfnApp.addPropertyOverride(
      "AccessToken",
      cdk.SecretValue.secretsManager(GITHUB_TOKEN_SECRET).unsafeUnwrap(),
    );
    cfnApp.addPropertyDeletionOverride("OauthToken");

    // RUNBOOK Lesson 5 — round 2. Newer @aws-cdk/aws-amplify-alpha
    // (≥ 2.251.x) auto-generates the App's service role with
    // `aws:SourceArn` + `aws:SourceAccount` conditions on the trust
    // policy, following AWS's "best practice" recommendation. In
    // practice those conditions break Amplify's internal service-role
    // chain (the runtime SourceArn doesn't match the obvious app
    // ARN), and builds fail in <1s with "Unable to assume specified
    // IAM Role."
    //
    // The first M2-B deploy got a conditions-free role and worked.
    // A subsequent CDK deploy in M3 caused CFN to recreate the role
    // (likely because the App resource diff propagated), this time
    // with the new alpha defaults, and every build after broke until
    // the trust policy was hand-stripped via
    // `aws iam update-assume-role-policy`.
    //
    // Override the role's trust policy to the minimal "amplify can
    // assume" form (matching the earlier project's working role). Future alpha
    // upgrades that re-add conditions will get overwritten on every
    // deploy.
    // The L2 alpha doesn't expose the auto-created service role via a
    // typed property, so reach into the construct tree by its known
    // child id ("Role"). If a future alpha rename breaks this lookup
    // we throw loudly rather than silently letting the conditions
    // come back.
    const webAppRole = webApp.node.tryFindChild("Role") as iam.Role | undefined;
    if (!webAppRole) {
      throw new Error(
        "Could not find webApp.node.findChild('Role'). The alpha module likely renamed it — re-locate the App's auto-generated service role and reapply the no-conditions trust override.",
      );
    }
    const cfnWebAppRole = webAppRole.node.defaultChild as iam.CfnRole;
    cfnWebAppRole.assumeRolePolicyDocument = {
      Version: "2012-10-17",
      Statement: [
        {
          Effect: "Allow",
          Principal: { Service: "amplify.amazonaws.com" },
          Action: "sts:AssumeRole",
        },
      ],
    };

    const mainBranch = webApp.addBranch("main", {
      autoBuild: true,
      stage: "PRODUCTION",
      branchName: "main",
    });

    // Grant the SSR compute role read access to the DDB table so
    // Server Components in `web/src/lib/dynamodb.ts` can scan ride
    // state at request time. Reads stay table-wide (no LeadingKeys
    // condition) because the parks pages legitimately read RIDE#*
    // rows the poller writes — only writes get the prefix scoping
    // below.
    if (!webApp.computeRole) {
      throw new Error(
        "Amplify L2 should auto-create computeRole when platform is WEB_COMPUTE. Got undefined.",
      );
    }
    dataTable.grantReadData(webApp.computeRole);

    // ─── M3: scoped write permissions on the SSR compute role ───────
    //
    // Defense-in-depth IAM scoping (Option B′ from RUNBOOK.md "Decision
    // to flag for Phase 1"). Restricts SSR-side writes to user-data
    // partitions only, so a bug in any /api/me/* handler cannot
    // corrupt the live RIDE# state the poller maintains.
    //
    // Partitions in scope:
    //   USER#<sub> → PROFILE, FAV_RIDE#<ride_id>  (M3 phases 1+2)
    //   PARK#<key> → USER#<sub>                   (park subscriptions)
    //
    // What this DOES enforce: the SSR role cannot write to RIDE#*
    // rows under any circumstances, even with a buggy handler.
    //
    // What this does NOT enforce: cross-user isolation. All SSR
    // requests use the same compute role, so one user's session
    // could in principle write another user's PK if the handler
    // forgets to constrain by `auth().sub`. Route handlers MUST
    // enforce that themselves. True per-user IAM isolation would
    // require Cognito Identity Pool + per-request
    // AssumeRoleWithWebIdentity — overkill for a trusted-user
    // portfolio app (documented as Option C in the runbook).
    //
    // ForAllValues:StringLike is the correct operator: dynamodb:LeadingKeys
    // is a multi-valued condition key (BatchWriteItem can target many
    // PKs), and we want every targeted PK to match one of the patterns.
    webApp.computeRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
        ],
        resources: [dataTable.tableArn],
        conditions: {
          "ForAllValues:StringLike": {
            "dynamodb:LeadingKeys": ["USER#*", "PARK#*"],
          },
        },
      }),
    );

    // M3: SSM read for the Pushover app token (used at SSR runtime
    // by /api/me/profile-style server actions to validate user-
    // supplied Pushover user keys before saving). Tightly scoped to
    // the one parameter ARN — same hygiene as the poller's grant.
    // Default-key SecureString decryption needs no explicit KMS
    // permission.
    webApp.computeRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter${PUSHOVER_APP_TOKEN_PARAM}`,
        ],
      }),
    );

    // CloudWatch Logs permissions for the SSR compute role. Newer
    // alpha versions of @aws-cdk/aws-amplify-alpha don't auto-attach
    // these (the earlier project's deploy at v2.251.0 picked up an
    // `AmplifyComputeLogs` policy automatically; MM's identical-version
    // deploy did NOT — likely an upstream change in how the L2 wires
    // default policies when the user adds their own). Without these,
    // builds fail with "Unable to assume specified IAM Role" because
    // Amplify pre-validates the role can write its expected logs.
    webApp.computeRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ],
        resources: [
          `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/amplify/*`,
        ],
      }),
    );

    // Custom domain. Amplify auto-issues a us-east-1 cert for the
    // subdomain on first deploy and emits a DNS-validation CNAME
    // visible in the Amplify console — see the AmplifyDomainStatus
    // output below for how to find it. Cloudflare DNS-only (gray
    // cloud), proxied = OFF.
    const webAppCustomDomain = webApp.addDomain("WebAppCustomDomain", {
      domainName: "megillini.dev",
      subDomains: [{ branch: mainBranch, prefix: "magicmonitor" }],
    });

    // ─── GitHub Actions OIDC role ────────────────────────────────────
    // Imports the existing OIDC provider (created by an earlier project —
    // AWS only allows one provider per token URL per account). The
    // role itself is MM-specific and trust-policy-scoped to pushes/PRs
    // on illinigirl/MagicMonitor only.
    const githubOidc = iam.OpenIdConnectProvider.fromOpenIdConnectProviderArn(
      this,
      "GithubOidcImported",
      GITHUB_OIDC_PROVIDER_ARN,
    );

    const deployRole = new iam.Role(this, "GithubDeployRole", {
      roleName: "MagicMonitorGithubDeploy",
      assumedBy: new iam.FederatedPrincipal(
        githubOidc.openIdConnectProviderArn,
        {
          StringEquals: {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          },
          StringLike: {
            "token.actions.githubusercontent.com:sub": [
              `repo:${GITHUB_OWNER}/${GITHUB_REPO}:ref:refs/heads/main`,
              `repo:${GITHUB_OWNER}/${GITHUB_REPO}:pull_request`,
            ],
          },
        },
        "sts:AssumeRoleWithWebIdentity",
      ),
      description:
        "Assumed by GitHub Actions to deploy the MagicMonitor CDK stack",
      maxSessionDuration: cdk.Duration.hours(1),
    });

    // AdministratorAccess at this scale is fine for a single-developer
    // portfolio project; can narrow to per-service permissions later
    // if MM ever has multi-developer CI.
    deployRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName("AdministratorAccess"),
    );

    // ─── Outputs (M2-B) ──────────────────────────────────────────────
    new cdk.CfnOutput(this, "AmplifyAppId", {
      value: webApp.appId,
      description: "Amplify app id (find in console)",
    });
    new cdk.CfnOutput(this, "AmplifyDefaultUrl", {
      value: `https://${mainBranch.branchName}.${webApp.defaultDomain}`,
      description: "Default *.amplifyapp.com URL — works alongside the custom domain",
    });
    new cdk.CfnOutput(this, "AmplifyCustomUrl", {
      value: `https://${APP_DOMAIN}`,
      description: "Public URL (custom domain) — primary user-facing URL",
    });
    new cdk.CfnOutput(this, "AmplifyDomainStatus", {
      value: webAppCustomDomain.domainName,
      description: "Custom domain attached to Amplify app — see console for production CNAME target",
    });
    new cdk.CfnOutput(this, "CognitoClientId", {
      value: userPoolClient.userPoolClientId,
      description: "Magic Monitor's Cognito app client (separate from the earlier project's)",
    });
    new cdk.CfnOutput(this, "CognitoIssuer", {
      value: cognitoIssuer,
      description: "OIDC issuer URL — NextAuth uses this to fetch JWKS",
    });
    new cdk.CfnOutput(this, "CognitoDomainUrl", {
      value: COGNITO_DOMAIN_URL,
      description: "Cognito hosted-UI base URL (shared with an earlier project)",
    });
    new cdk.CfnOutput(this, "GithubDeployRoleArn", {
      value: deployRole.roleArn,
      description: "Role ARN to set as AWS_ROLE_ARN in MagicMonitor's GitHub Actions secrets",
    });
  }
}
