#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { DisneyStack } from "../lib/disney-stack";

const app = new cdk.App();

new DisneyStack(app, "DisneyStack", {
  env: {
    // Pinned to the same account+region as Watchtower so the SSO session
    // and Cloudflare DNS workflow you already use Just Work.
    account: "601669029997",
    region: "us-east-2",
  },
  description: "Disney parks ride-status alerter — phase 1 (poller + Pushover)",
});
