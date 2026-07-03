// Tests for sendTestNotification: identity gate, needs-a-saved-key,
// send + soft-fail, and the double-tap debounce.
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

const auth = vi.fn();
vi.mock("@/auth", () => ({ auth: () => auth() }));

const getUserProfile = vi.fn();
vi.mock("@/lib/dynamodb-writes", () => ({
  getUserProfile: (...a: unknown[]) => getUserProfile(...a),
  // Unused by these tests but imported by the module under test.
  getUserParkSubscriptions: vi.fn(),
  putUserProfile: vi.fn(),
  setParkSubscription: vi.fn(),
}));

const sendPushoverMessage = vi.fn();
vi.mock("@/lib/pushover", () => ({
  sendPushoverMessage: (...a: unknown[]) => sendPushoverMessage(...a),
  validatePushoverUserKey: vi.fn(),
}));

const KEY = "a".repeat(30);

beforeEach(() => {
  vi.clearAllMocks();
  auth.mockResolvedValue({ user: { id: "sub-1" } });
  getUserProfile.mockResolvedValue({ pushoverUserKey: KEY });
  sendPushoverMessage.mockResolvedValue(undefined);
});

describe("sendTestNotification", () => {
  it("rejects when not signed in", async () => {
    auth.mockResolvedValue(null);
    const { sendTestNotification } = await import("./actions");
    expect(await sendTestNotification()).toEqual({
      ok: false,
      error: "Not signed in.",
    });
    expect(sendPushoverMessage).not.toHaveBeenCalled();
  });

  it("requires a saved Pushover key", async () => {
    auth.mockResolvedValue({ user: { id: "sub-nokey" } });
    getUserProfile.mockResolvedValue({});
    const { sendTestNotification } = await import("./actions");
    const res = await sendTestNotification();
    expect(res.ok).toBe(false);
    expect(sendPushoverMessage).not.toHaveBeenCalled();
  });

  it("sends to the SAVED key and returns ok", async () => {
    auth.mockResolvedValue({ user: { id: "sub-ok" } });
    const { sendTestNotification } = await import("./actions");
    expect(await sendTestNotification()).toEqual({ ok: true });
    expect(sendPushoverMessage).toHaveBeenCalledOnce();
    expect(sendPushoverMessage.mock.calls[0][0]).toBe(KEY);
  });

  it("returns a soft error (not a throw) when Pushover send fails", async () => {
    auth.mockResolvedValue({ user: { id: "sub-fail" } });
    sendPushoverMessage.mockRejectedValue(new Error("bad key"));
    const { sendTestNotification } = await import("./actions");
    const res = await sendTestNotification();
    expect(res.ok).toBe(false);
  });

  it("debounces rapid double-taps for the same user", async () => {
    auth.mockResolvedValue({ user: { id: "sub-debounce" } });
    const { sendTestNotification } = await import("./actions");
    expect(await sendTestNotification()).toEqual({ ok: true });
    const second = await sendTestNotification();
    expect(second.ok).toBe(false);
    expect(sendPushoverMessage).toHaveBeenCalledOnce(); // second suppressed
  });
});
