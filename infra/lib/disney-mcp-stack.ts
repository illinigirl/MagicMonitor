import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as iam from "aws-cdk-lib/aws-iam";
import * as apigwv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as apigwv2_integ from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as path from "path";
import { execSync } from "child_process";
import * as fs from "fs";

/**
 * SSM SecureString param holding the v1 bearer secret. Bootstrapped
 * manually (one-time `aws ssm put-parameter`) so the secret never
 * lives in CDK, CloudFormation, or git. To create:
 *
 *   aws ssm put-parameter --profile watchtower --region us-east-2 \
 *     --name /disney/mcp/bearer_secret \
 *     --type SecureString \
 *     --value "$(openssl rand -base64 32)"
 *
 * The Lambda's IAM role grants ssm:GetParameter on exactly this name
 * (no wildcards) and the Lambda fetches the value at cold-start init.
 */
const MCP_BEARER_SECRET_PARAM = "/disney/mcp/bearer_secret";

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
 *   • Rollback path is `cdk destroy DisneyMcpStack` — removes
 *     every resource this stack created and nothing else
 *
 * v1 auth: shared bearer secret in SSM SecureString, fetched by the
 * Lambda at cold-start init. NOT production-grade — just enough to
 * prove the transport + IAM wiring. Session 2 replaces this with
 * Cognito OAuth + a DCR proxy so Claude mobile's OAuth-only flow
 * can connect.
 *
 * v1 IAM: DDB Read only. No write tools port over yet, so the
 * Lambda role doesn't need PutItem/UpdateItem. Hardens the blast
 * radius of any auth bug in session 1.
 */
export class DisneyMcpStack extends cdk.Stack {
  public readonly apiUrl: string;
  public readonly lambdaFunctionName: string;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, { env: DEPLOY_ENV, ...props });

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
      // 30s budget. Worst case is the paginated Scan in
      // get_park_live_status: ~50ms per page × a few pages = under 1s.
      // 30s leaves headroom for cold start + transient DDB latency.
      timeout: cdk.Duration.seconds(30),
      tracing: lambda.Tracing.ACTIVE,
      environment: {
        DISNEY_TABLE_NAME: DDB_TABLE_NAME,
        DISNEY_REGION: this.region,
        // Parameter name (not value!) so the handler can fetch the
        // SecureString at cold-start. Value never enters CFN.
        MCP_BEARER_SECRET_PARAM,
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

    // ─── IAM: SSM SecureString read ────────────────────────────────
    // Scoped to the exact parameter ARN. SecureString decrypt is
    // granted via the AWS-managed KMS alias for SSM, so no explicit
    // KMS permission needed (matches the Pushover-secret pattern in
    // disney-stack.ts).
    mcpFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter", "ssm:GetParameters"],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter${MCP_BEARER_SECRET_PARAM}`,
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
      // No throttling at the API Gateway layer for v1 — bearer-token
      // gate + per-user Cognito allowlist (session 2) will be the
      // primary rate-limit story. Lambda's account-wide concurrency
      // cap (10) is the ultimate ceiling.
    });

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

    // ─── Outputs ───────────────────────────────────────────────────
    new cdk.CfnOutput(this, "McpApiUrl", {
      value: httpApi.apiEndpoint,
      description: "Base URL for the MCP HTTPS endpoint (smoke-test with curl + bearer header)",
    });
    new cdk.CfnOutput(this, "McpFunctionName", {
      value: mcpFn.functionName,
      description: "Lambda function name (CloudWatch Logs / aws logs tail)",
    });
    new cdk.CfnOutput(this, "McpLogGroup", {
      value: `/aws/lambda/${mcpFn.functionName}`,
      description: "CloudWatch Logs group for the MCP Lambda",
    });
    new cdk.CfnOutput(this, "McpBearerSecretParam", {
      value: MCP_BEARER_SECRET_PARAM,
      description: "SSM SecureString parameter holding the v1 bearer secret",
    });
  }
}
