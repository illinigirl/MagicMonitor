#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { DisneyStack } from "../lib/disney-stack";
import { DisneyMcpStack } from "../lib/disney-mcp-stack";

const app = new cdk.App();

new DisneyStack(app, "DisneyStack", {
  env: {
    // Pinned to the same account+region as an earlier project so the SSO
    // session and Cloudflare DNS workflow you already use Just Work.
    account: "601669029997",
    region: "us-east-2",
  },
  description: "Disney parks ride-status alerter — phase 1 (poller + Pushover)",
});

// Net-new stack for the HTTPS MCP transport (M9 Phase 1). Intentionally
// independent of DisneyStack so it can be deployed / destroyed without
// touching any of the customer-facing resources. References the
// DisneyData table by name rather than via cross-stack ref.
new DisneyMcpStack(app, "DisneyMcpStack", {
  description: "HTTPS MCP transport for Claude mobile — M9 Phase 1, session 1 (bearer-token v1)",
});
