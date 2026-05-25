// Unit tests for getParkRides() — the read path that powers each
// park's dashboard page.
//
// History: this function was the site of the 2026-05-24 silent
// regression where a single-page DDB Scan stopped returning STATE
// rows once WAIT# rows pushed past page 1. The immediate fix
// added pagination; the structural fix added a GSI on park_key
// and switched Scan → Query. These tests are the test-time layer
// of the three-layer defense documented in TESTING.md "Silent
// regressions from data growth": they exercise the pagination
// loop and pin the GSI name/key-condition contract so future
// drifts get caught at PR time, not in production.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// `server-only` throws at import time outside server contexts. Mock
// it to a no-op so the module-under-test can load cleanly in the
// Vitest node environment.
vi.mock("server-only", () => ({}));

// Capture the `send` calls so each test can stub responses and
// assert the right command was issued.
const sendMock = vi.fn();

// Replace the DDB DocumentClient with one whose `send` is our spy.
// The `client = ... DynamoDBDocumentClient.from(...)` line in
// dynamodb.ts becomes our mock client, and every `client.send(cmd)`
// call lands on sendMock.
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
  // The dynamodb.ts module caches the client on globalThis in non-
  // production environments. Reset between tests so each test gets
  // a freshly constructed (mocked) client.
  delete (globalThis as { __ddbClient?: unknown }).__ddbClient;
  vi.resetModules();
});

afterEach(() => {
  vi.restoreAllMocks();
});

function makeStateItem(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    ride_id: "ride-abc",
    park_key: "magic_kingdom",
    park_name: "Magic Kingdom",
    name: "Test Ride",
    status: "OPERATING",
    wait_mins: 15,
    last_seen: "2026-05-25T12:00:00Z",
    ll: null,
    ...overrides,
  };
}

describe("getParkRides", () => {
  it("returns items from a single-page response, sorted by name", async () => {
    sendMock.mockResolvedValueOnce({
      Items: [
        makeStateItem({ name: "Tomorrowland Speedway", ride_id: "r2" }),
        makeStateItem({ name: "Astro Orbiter", ride_id: "r1" }),
      ],
      LastEvaluatedKey: undefined,
    });

    const { getParkRides } = await import("./dynamodb");
    const items = await getParkRides("magic_kingdom");

    expect(items).toHaveLength(2);
    expect(items[0].name).toBe("Astro Orbiter");
    expect(items[1].name).toBe("Tomorrowland Speedway");
    expect(sendMock).toHaveBeenCalledTimes(1);
  });

  it("accumulates across paginated responses (defense for the 2026-05-24 regression category)", async () => {
    // Simulate a partition that's outgrown a single 1MB page: first
    // page returns some items + a LastEvaluatedKey; second page
    // returns the rest + no LastEvaluatedKey. The function must
    // walk all pages, otherwise we re-introduce the silent-empty
    // bug that lived in production for ~7 days.
    sendMock
      .mockResolvedValueOnce({
        Items: [makeStateItem({ name: "Big Thunder Mountain", ride_id: "r1" })],
        LastEvaluatedKey: { PK: "p1", SK: "STATE" },
      })
      .mockResolvedValueOnce({
        Items: [makeStateItem({ name: "Astro Orbiter", ride_id: "r2" })],
        LastEvaluatedKey: undefined,
      });

    const { getParkRides } = await import("./dynamodb");
    const items = await getParkRides("magic_kingdom");

    expect(items).toHaveLength(2);
    // Sorted by name: Astro before Big.
    expect(items.map((r) => r.name)).toEqual([
      "Astro Orbiter",
      "Big Thunder Mountain",
    ]);
    expect(sendMock).toHaveBeenCalledTimes(2);

    // Second call must have passed the LastEvaluatedKey from the
    // first response — that's the actual pagination contract.
    const secondCallCommand = sendMock.mock.calls[1][0];
    expect(secondCallCommand.input.ExclusiveStartKey).toEqual({
      PK: "p1",
      SK: "STATE",
    });
  });

  it("queries the park_key-SK GSI with the correct key condition", async () => {
    // Pins the GSI contract: index name, key-condition shape, and
    // expression-attribute values. Catches accidental regressions
    // back to a Scan or a typo in the index name. The GSI was added
    // in the 2026-05-25 CDK deploy; this test ensures the reader
    // continues to use it.
    sendMock.mockResolvedValueOnce({ Items: [], LastEvaluatedKey: undefined });

    const { getParkRides } = await import("./dynamodb");
    await getParkRides("epcot");

    const sentCommand = sendMock.mock.calls[0][0];
    expect(sentCommand.input.IndexName).toBe("park_key-SK-index");
    expect(sentCommand.input.KeyConditionExpression).toBe(
      "park_key = :p AND SK = :sk",
    );
    expect(sentCommand.input.ExpressionAttributeValues).toEqual({
      ":sk": "STATE",
      ":p": "epcot",
    });
  });

  it("returns empty array when partition has no STATE rows", async () => {
    sendMock.mockResolvedValueOnce({ Items: [], LastEvaluatedKey: undefined });

    const { getParkRides } = await import("./dynamodb");
    const items = await getParkRides("hollywood_studios");

    expect(items).toEqual([]);
  });
});
