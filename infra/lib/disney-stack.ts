import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as path from "path";
import { execSync } from "child_process";
import * as fs from "fs";

/** SSM parameter names for the Pushover credentials. Bootstrapped manually
 * (one-time `aws ssm put-parameter`) so secrets never live in CDK,
 * CloudFormation, or git. Same hygiene pattern as Watchtower's TMDB key. */
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

/**
 * Local Python bundling for the poller Lambda. Same approach as
 * Watchtower — uses host python3 to install deps with manylinux wheels,
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
        HISTORY_RETENTION_DAYS: "90",
      },
      // No reserved concurrency — account-wide cap is 10 and
      // Watchtower already uses ~3-4 concurrent slots. The poller
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

    // ─── Outputs ─────────────────────────────────────────────────────
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
  }
}
