import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as apigwv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as apigwv2_integ from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cw_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as sns from "aws-cdk-lib/aws-sns";
import * as subscriptions from "aws-cdk-lib/aws-sns-subscriptions";
import * as path from "path";
import { execSync } from "child_process";
import * as fs from "fs";

/**
 * DDB table name. Owned by DisneyStack — referenced here by name
 * rather than imported by ARN so this stack stays loosely coupled.
 * If DisneyStack ever renames the table, update here too.
 */
const DDB_TABLE_NAME = "DisneyData";

/**
 * AWS account + region. Pinned to DisneyStack's values so SSO,
 * profiles, and DNS workflows all match.
 */
const DEPLOY_ENV = { account: "601669029997", region: "us-east-2" };

/**
 * Cognito user pool that owns Magic Monitor's auth. Pre-existing (from
 * an earlier project) — referenced here by ID so this stack doesn't take a cross-
 * stack reference. The MCP Lambda's role is granted scoped
 * CreateUserPoolClient on this exact pool ARN for the DCR proxy.
 */
const COGNITO_USER_POOL_ID = "us-east-2_ORhu761AY";

/**
 * Cognito hosted-UI base URL. Owned by an earlier project — clients hit
 * `/oauth2/authorize` and `/oauth2/token` here directly via the OAuth
 * authorization-server metadata.
 */
const COGNITO_DOMAIN_URL = "https://auth.megillini.dev";

/**
 * S3 bucket holding the analytics snapshot + short-wait baselines for
 * the read-side analytics tools (session 2.5). Deterministic name
 * (account-suffixed for global uniqueness) so the nightly aggregator
 * GitHub Action can `aws s3 cp` to it without a CloudFormation lookup.
 *
 * Why S3 vs bundling into the Lambda asset: the snapshot regenerates
 * nightly but the Lambda only redeploys on code change (and is meant
 * to stop changing once stable). Bundling would freeze the data at the
 * last deploy; S3 lets a Lambda cold start pick up the latest nightly
 * regen with no redeploy. See server_http.py's `_snapshot()` docstring.
 */
const MCP_DATA_BUCKET_NAME = `magic-monitor-mcp-data-${DEPLOY_ENV.account}`;
const MCP_SNAPSHOT_KEY = "analytics-snapshot.json";
const MCP_BASELINES_KEY = "baselines.json";

/**
 * Local Python bundling for the MCP Lambda. Same approach as the
 * poller Lambda in disney-stack.ts (manylinux wheels for cross-
 * compile, falls back to Docker if python3 isn't available). The
 * skip-list excludes test/cache/venv directories that don't need
 * to ship.
 */
function bundleMcpAsset(assetPath: string): lambda.AssetCode {
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
          // Keep the asset tight: don't ship local dev artifacts. The
          // .venv is the big one — it's a full local dev environment,
          // not Lambda layout.
          const skip = new Set([
            "requirements.txt",
            ".venv",
            "evals",
            "tests",
            "pytest.ini",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".env",
            ".env.example",
            "README.md",
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
 * DisneyMcpStack — M9 Phase 1, session 1.
 *
 * Net-new AWS surface for the HTTPS MCP transport so Claude mobile
 * (and other remote MCP clients) can hit the same data plane that
 * the stdio MCP server exposes to Claude Desktop.
 *
 * Designed to be entirely separable from DisneyStack:
 *   • Reads DisneyData by name, doesn't take a cross-stack ref
 *   • Doesn't touch the Amplify app, poller Lambda, user pool, or
 *     anything else DisneyStack owns
 *   • Rollback path is `cdk destroy DisneyMcpStack` — removes every
 *     resource this stack created EXCEPT the RETAIN'd analytics bucket
 *     (kept deliberately, see its comment). Note: because that bucket has
 *     a deterministic fixed name, a later re-deploy of this stack will
 *     FAIL on a name collision until the retained bucket is deleted or
 *     `cdk import`ed back in. Re-create procedure: delete (or import) the
 *     retained bucket first.
 *
 * Auth (2B): Cognito access-token JWTs verified per request against
 * the user pool's JWKS, gated by an allowlist of `sub` UUIDs bound
 * at deploy time via CDK context (`mcp_allowed_subs`). DCR proxy
 * translates RFC 7591 /register calls into CreateUserPoolClient on
 * the shared pool. The earlier shared-bearer-secret path was hard-
 * replaced; no dual-auth.
 *
 * v1 IAM: DDB Read + Cognito CreateUserPoolClient on the one pool.
 * No DDB writes (no write tools port over yet — those land alongside
 * scoped per-user write IAM in session 3+).
 */
export class DisneyMcpStack extends cdk.Stack {
  public readonly apiUrl: string;
  public readonly lambdaFunctionName: string;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, { env: DEPLOY_ENV, ...props });

    // ─── Allowlist: who can call the MCP server ─────────────────────
    // Comma-separated Cognito `sub` UUIDs, sourced from CDK context
    // (`-c mcp_allowed_subs=...` or cdk.json's context block). The
    // values are public identifiers (not secrets), so cdk.json is
    // the right home. Empty/undefined → deny-all, which is the safe
    // default if context is forgotten on a fresh deploy.
    const allowedSubsRaw = this.node.tryGetContext("mcp_allowed_subs");
    const allowedSubs: string =
      typeof allowedSubsRaw === "string" ? allowedSubsRaw : "";

    // ─── Sub → friendly-id map (write-tool attribution, M5) ─────────
    // "sub1:megan,sub2:jim" from CDK context. In the SHARED trip model
    // this only labels who recorded a plan (created_by) — it does NOT
    // route partitions. Public identifiers, so cdk.json is the right
    // home. An allowlisted-but-unmapped sub still writes (labeled by raw
    // sub); the gate is the allowlist, not this map.
    const subUserMapRaw = this.node.tryGetContext("mcp_sub_user_map");
    const subUserMap: string =
      typeof subUserMapRaw === "string" ? subUserMapRaw : "";

    // ─── S3: analytics data bucket ─────────────────────────────────
    // Holds the snapshot + baselines uploaded nightly by the aggregator
    // action. Private, SSL-enforced; the Lambda reads, the GitHub
    // deploy role writes (it already has AdministratorAccess, so no
    // bucket policy / grant is needed on the write side). RETAIN on
    // stack delete so a `cdk destroy` doesn't drop the data — the
    // nightly action repopulates it anyway, but retaining avoids a
    // cold-start gap if the stack is ever recreated.
    const dataBucket = new s3.Bucket(this, "McpDataBucket", {
      bucketName: MCP_DATA_BUCKET_NAME,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ─── Lambda function ────────────────────────────────────────────
    const mcpFn = new lambda.Function(this, "McpHttpFunction", {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.X86_64,
      handler: "lambda_handler.handler",
      code: bundleMcpAsset(path.join(__dirname, "../../mcp")),
      // 512MB matches the poller. MCP tool calls are mostly DDB I/O
      // wait, not CPU. Cold start: Mangum + Starlette + FastMCP +
      // boto3 = ~1.5-2.5s, acceptable for an MCP server that
      // typically runs hot once mobile connects.
      memorySize: 512,
      // 30s — capped to match the API Gateway HTTP API integration
      // timeout (30s default max; raising it needs a service-quota
      // increase). A longer Lambda timeout would be moot: APIGW would
      // 504 the client at 30s while the Lambda kept burning compute.
      // get_planning_context (M5) is the heaviest tool — three sequential
      // upstream HTTPS calls (themeparks.wiki showtimes + hours, Open-
      // Meteo weather, each 10s-capped) plus per-ride DDB + a park-wide
      // DOWN scan. Typical runtime ~2-5s; the only way to approach 30s is
      // multiple upstreams hanging to their 10s cap at once. If that tail
      // ever bites, parallelize those fetches (or trim per-call timeouts)
      // rather than raising this — the APIGW ceiling is the real limit.
      timeout: cdk.Duration.seconds(30),
      tracing: lambda.Tracing.ACTIVE,
      environment: {
        DISNEY_TABLE_NAME: DDB_TABLE_NAME,
        DISNEY_REGION: this.region,
        // Cognito config for jwt_verifier + dcr_proxy. None of these
        // are secrets — pool IDs, region names, domain URLs, and
        // Cognito sub UUIDs are public identifiers — so they ride in
        // plain Lambda env vars rather than SSM.
        COGNITO_USER_POOL_ID,
        COGNITO_REGION: this.region,
        COGNITO_DOMAIN_URL,
        MCP_ALLOWED_SUBS: allowedSubs,
        // Write-tool attribution map (M5).
        MCP_SUB_USER_MAP: subUserMap,
        // Analytics snapshot delivery (session 2.5). The Lambda fetches
        // these from S3 lazily on first analytics tool call.
        MCP_SNAPSHOT_BUCKET: MCP_DATA_BUCKET_NAME,
        MCP_SNAPSHOT_KEY,
        MCP_BASELINES_KEY,
        // MCP_PUBLIC_BASE_URL is added below via addEnvironment once
        // the HTTP API is constructed (chicken-and-egg: the Lambda
        // needs to know its own public URL for OAuth metadata, but
        // the URL only exists after the API is created).
      },
    });

    this.lambdaFunctionName = mcpFn.functionName;

    // ─── IAM: DDB read-only ────────────────────────────────────────
    // Tightly scoped: GetItem + Query + Scan on the single table and
    // its indexes. No write actions. No wildcards beyond the table's
    // own index ARN pattern.
    mcpFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:BatchGetItem",
        ],
        resources: [
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${DDB_TABLE_NAME}`,
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${DDB_TABLE_NAME}/index/*`,
        ],
      }),
    );

    // ─── IAM: scoped DDB write (plan/trip tools, M5) ───────────────
    // PutItem/UpdateItem/DeleteItem constrained to USER#* / PARK#*
    // leading keys — mirrors the web SSR computeRole grant in
    // disney-stack.ts exactly. A shared compute role can't enforce
    // PER-USER isolation in IAM (all requests use this one role), so the
    // partition is enforced in code (writes go to the shared USER#megan
    // space, identity is attribution only); LeadingKeys is defense-in-
    // depth so a bug can't write RIDE#/STATE/HIST# rows. True per-user
    // IAM isolation would need a Cognito Identity Pool + per-request
    // AssumeRoleWithWebIdentity — overkill for a trusted-family app.
    //
    // ForAllValues:StringLike is the correct operator: dynamodb:LeadingKeys
    // is multi-valued (BatchWriteItem can target many PKs) and we want
    // every targeted PK to match a pattern.
    mcpFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          // create_trip writes the trip header + dormant day-plans in one
          // all-or-nothing batch. The LeadingKeys condition below already
          // covers the multi-PK case (see the note above); the action was
          // just missing — create_trip got AccessDenied in prod until this
          // was added.
          "dynamodb:BatchWriteItem",
        ],
        resources: [
          `arn:aws:dynamodb:${this.region}:${this.account}:table/${DDB_TABLE_NAME}`,
        ],
        conditions: {
          "ForAllValues:StringLike": {
            // MCPCLIENT#* — DCR registered-client marker rows (#8). The
            // /register handler writes one per minted client; the auth
            // middleware GetItems it (read is covered by the read grant).
            "dynamodb:LeadingKeys": ["USER#*", "PARK#*", "MCPCLIENT#*"],
          },
        },
      }),
    );

    // ─── IAM: S3 read on the analytics data bucket ─────────────────
    // GetObject only, scoped to this one bucket. grantRead wires the
    // bucket policy + the role permission in one call.
    dataBucket.grantRead(mcpFn);

    // ─── IAM: Cognito CreateUserPoolClient (DCR proxy) ─────────────
    // Scoped to the one shared pool. Each /register call creates a
    // new app client on the pool; no client is ever deleted by this
    // role (Cognito's 1000-client limit is irrelevant at 3 users +
    // occasional reinstalls — deferred per locked decision).
    mcpFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["cognito-idp:CreateUserPoolClient"],
        resources: [
          `arn:aws:cognito-idp:${this.region}:${this.account}:userpool/${COGNITO_USER_POOL_ID}`,
        ],
      }),
    );

    // ─── API Gateway HTTP API ──────────────────────────────────────
    // HTTP API is cheaper + faster than REST API and exactly what we
    // need: a single Lambda integration with no transformation. The
    // MCP streamable-HTTP transport receives all client requests on
    // one POST endpoint (/mcp by default) and a GET endpoint for
    // server-sent events. We mount the whole Lambda at the API root
    // and let FastMCP route internally.
    const httpApi = new apigwv2.HttpApi(this, "McpHttpApi", {
      apiName: "magic-monitor-mcp",
      description: "HTTPS transport for the Magic Monitor MCP server (M9 Phase 1)",
    });

    // Stage-level throttle. JWT verification happens INSIDE the Lambda,
    // so unauthenticated/rejected requests still consume one of the
    // account's 10 concurrency slots before the 401 — the auth gate
    // cannot be the rate-limit story for resource EXHAUSTION (only for
    // access). The unauthenticated /register and discovery routes do real
    // in-Lambda work too. Without a throttle, a flood of anonymous
    // requests to the public URL can starve the every-2-min poller in the
    // sibling stack, which shares the same account-wide cap. Cap the
    // request rate well below that exhaustion point. These numbers are a
    // starting point — raise them if a legit planning session (many
    // sequential tool calls) ever returns 429s.
    const defaultStage = httpApi.defaultStage?.node
      .defaultChild as apigwv2.CfnStage;
    defaultStage.defaultRouteSettings = {
      throttlingRateLimit: 10,
      throttlingBurstLimit: 20,
    };

    const lambdaIntegration = new apigwv2_integ.HttpLambdaIntegration(
      "McpLambdaIntegration",
      mcpFn,
    );

    // Catch-all proxy — every method, every path → Lambda. FastMCP's
    // streamable-HTTP app routes internally based on
    // Starlette routes; API Gateway just passes the request through.
    httpApi.addRoutes({
      path: "/{proxy+}",
      methods: [apigwv2.HttpMethod.ANY],
      integration: lambdaIntegration,
    });
    // Root path needs to be wired separately — the {proxy+} pattern
    // doesn't match the empty path.
    httpApi.addRoutes({
      path: "/",
      methods: [apigwv2.HttpMethod.ANY],
      integration: lambdaIntegration,
    });

    this.apiUrl = httpApi.apiEndpoint;

    // Wire the public base URL back to the Lambda env so the OAuth
    // metadata endpoints can advertise their own resource + issuer.
    mcpFn.addEnvironment("MCP_PUBLIC_BASE_URL", httpApi.apiEndpoint);

    // ─── Monitoring ────────────────────────────────────────────────
    const mcpAlarmTopic = new sns.Topic(this, "McpAlarmTopic", {
      topicName: "magic-monitor-mcp-alarms",
    });
    // Optional notify target, supplied at deploy time (no PII in source):
    // `cdk deploy -c alarmEmail=you@example.com`.
    const alarmEmail = this.node.tryGetContext("alarmEmail");
    if (alarmEmail) {
      mcpAlarmTopic.addSubscription(
        new subscriptions.EmailSubscription(alarmEmail),
      );
    }

    // Duration p95 alarm: several live tools full-table-Scan a multi-GB
    // table that grows every 2 min, so latency creeps toward the 30s API
    // Gateway hard cap (see the data-growth review finding). This is the
    // runtime stop-loss before users start seeing 504s — pairs with
    // migrating those Scans to the park_key-SK GSI.
    mcpFn
      .metricDuration({ period: cdk.Duration.minutes(5), statistic: "p95" })
      .createAlarm(this, "McpDurationAlarm", {
        alarmName: "magic-monitor-mcp-slow",
        alarmDescription:
          "MCP Lambda p95 duration >10s — live Scans approaching the 30s cap.",
        threshold: cdk.Duration.seconds(10).toMilliseconds(),
        evaluationPeriods: 3,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      })
      .addAlarmAction(new cw_actions.SnsAction(mcpAlarmTopic));

    // Error alarm: a 5xx-raising MCP handler (verifier misconfig, JWKS
    // outage, unhandled tool exception) surfaces instead of failing silent.
    mcpFn
      .metricErrors({ period: cdk.Duration.minutes(5) })
      .createAlarm(this, "McpErrorsAlarm", {
        alarmName: "magic-monitor-mcp-errors",
        alarmDescription: "MCP Lambda raised >=1 error in a 5-minute window.",
        threshold: 1,
        evaluationPeriods: 1,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      })
      .addAlarmAction(new cw_actions.SnsAction(mcpAlarmTopic));

    // ─── Outputs ───────────────────────────────────────────────────
    new cdk.CfnOutput(this, "McpApiUrl", {
      value: httpApi.apiEndpoint,
      description: "Base URL for the MCP HTTPS endpoint (curl OAuth dance starts here)",
    });
    new cdk.CfnOutput(this, "McpFunctionName", {
      value: mcpFn.functionName,
      description: "Lambda function name (CloudWatch Logs / aws logs tail)",
    });
    new cdk.CfnOutput(this, "McpDataBucketName", {
      value: dataBucket.bucketName,
      description:
        "S3 bucket the nightly aggregator uploads the snapshot + baselines to",
    });
    new cdk.CfnOutput(this, "McpLogGroup", {
      value: `/aws/lambda/${mcpFn.functionName}`,
      description: "CloudWatch Logs group for the MCP Lambda",
    });
    new cdk.CfnOutput(this, "McpAllowedSubsCount", {
      value: String(
        allowedSubs.split(",").map((s) => s.trim()).filter(Boolean).length,
      ),
      description:
        "How many Cognito subs are allowlisted (sanity check after deploy — should match expected user count)",
    });
  }
}
