// Unit tests for the per-user write module — focused on
// getUserParkSubscriptions, which until 2026-06-11 was a single-page
// Scan + FilterExpression: the exact shape of the 2026-05-24
// getParkRides regression. On a multi-GB table the user's
// PARK#<key>/USER#<sub> rows are almost never in the first 1MB scan
// page, so the function silently returned an empty set — rendering
// /me toggles unchecked AND (worse) making saveSettings' diff treat
// every park as "not subscribed", so unsubscription silently no-oped.
//
// The fix issues one GetItem per park on the fully-known key. These
// tests pin that contract (GetItem, not Scan; correct keys; result
// independent of table size) so a future drift back to a Scan is
// caught at PR time. This is the test-time layer of the three-layer
// defense in TESTING.md "Silent regressions from data growth".
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// `server-only` throws at import time outside a server context. Mock
// it to a no-op so the module loads in the Vitest node environment.
vi.mock("server-only", () => ({}));

const sendMock = vi.fn();

// Same module-level mock as dynamodb.test.ts: replace the DocumentClient
// factory so `client.send(cmd)` lands on our spy.
vi.mock("@aws-sdk/lib-dynamodb", async () => {
  const actual = await vi.importActual<typeof import("@aws-sdk/lib-dynamodb")>(
    "@aws-sdk/lib-dynamodb",
  );
  return {
    ...actual,
    DynamoDBDocumentClient: {
      ...actual.DynamoDBDocumentClient,
      from: () => ({ send: sendMock }),
    },
  };
});

beforeEach(() => {
  sendMock.mockReset();
  delete (globalThis as { __ddbClient?: unknown }).__ddbClient;
  vi.resetModules();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("getUserParkSubscriptions", () => {
  it("issues one GetItem per park on the exact PARK#/USER# key (never a Scan)", async () => {
    // Every park returns no item → empty set, but we care about the
    // commands issued.
    sendMock.mockResolvedValue({ Item: undefined });

    const { getUserParkSubscriptions } = await import("./dynamodb-writes");
    const { PARKS } = await import("./parks");
    await getUserParkSubscriptions("sub-123");

    // One read per park, and each one is a keyed GetItem — not a Scan.
    expect(sendMock).toHaveBeenCalledTimes(PARKS.length);
    const sentKeys = sendMock.mock.calls.map((c) => c[0].input.Key);
    for (const park of PARKS) {
      expect(sentKeys).toContainEqual({
        PK: `PARK#${park.key}`,
        SK: "USER#sub-123",
      });
    }
    // No call carried a FilterExpression — proves we didn't regress to Scan.
    for (const call of sendMock.mock.calls) {
      expect(call[0].input.FilterExpression).toBeUndefined();
    }
  });

  it("returns exactly the parks whose row exists", async () => {
    // magic_kingdom + hollywood_studios subscribed; epcot + animal_kingdom not.
    sendMock.mockImplementation((cmd: { input: { Key: { PK: string } } }) => {
      const subscribed =
        cmd.input.Key.PK === "PARK#magic_kingdom" ||
        cmd.input.Key.PK === "PARK#hollywood_studios";
      return Promise.resolve({
        Item: subscribed
          ? { PK: cmd.input.Key.PK, SK: "USER#sub-123", subscribed_at: "x" }
          : undefined,
      });
    });

    const { getUserParkSubscriptions } = await import("./dynamodb-writes");
    const result = await getUserParkSubscriptions("sub-123");

    expect([...result].sort()).toEqual(["hollywood_studios", "magic_kingdom"]);
  });

  it("is independent of table size — a subscription is found regardless of how many other rows exist", async () => {
    // This is the regression guard. With the old Scan, the user's rows
    // could fall outside the first 1MB page on a large table and be
    // silently missed. A per-key GetItem cannot be affected by table
    // size, so a subscribed park is always returned. We model "large
    // table" as the GetItem still resolving the row directly.
    sendMock.mockImplementation((cmd: { input: { Key: { PK: string } } }) =>
      Promise.resolve({
        Item:
          cmd.input.Key.PK === "PARK#epcot"
            ? { PK: "PARK#epcot", SK: "USER#sub-123" }
            : undefined,
      }),
    );

    const { getUserParkSubscriptions } = await import("./dynamodb-writes");
    const result = await getUserParkSubscriptions("sub-123");

    expect(result.has("epcot")).toBe(true);
    expect(result.size).toBe(1);
  });

  it("returns an empty set when the user is subscribed to no parks", async () => {
    sendMock.mockResolvedValue({ Item: undefined });
    const { getUserParkSubscriptions } = await import("./dynamodb-writes");
    expect(await getUserParkSubscriptions("sub-123")).toEqual(new Set());
  });
});
