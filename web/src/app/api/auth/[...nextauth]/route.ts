/**
 * NextAuth route handler. Auth.js v5 exports a `handlers` object with
 * GET/POST from the config; we re-spread them here so the framework
 * picks them up at /api/auth/* (sign-in, callback, sign-out, csrf,
 * session, providers).
 */

import { handlers } from "@/auth";

export const { GET, POST } = handlers;
