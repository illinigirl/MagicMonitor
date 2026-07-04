import { describe, expect, it } from "vitest";

import { isAndroidUa, mapsUrl } from "./nav-link";

// Real ride_id from attraction-locations.json (Big Thunder).
const BTM = "de3309ca-97d5-4211-bffe-739fed47e92f";

describe("mapsUrl", () => {
  it("known ride → Apple Maps walking directions by default", () => {
    const url = mapsUrl({ ride_id: BTM, name: "Big Thunder" });
    expect(url).toMatch(/^https:\/\/maps\.apple\.com\/\?daddr=28\./);
    expect(url).toContain("dirflg=w");
  });

  it("known ride + android → Google Maps walking directions (native app)", () => {
    const url = mapsUrl({ ride_id: BTM, name: "Big Thunder", android: true });
    expect(url).toMatch(/^https:\/\/www\.google\.com\/maps\/dir\//);
    expect(url).toContain("travelmode=walking");
  });

  it("unknown place → name+park search, per platform", () => {
    const apple = mapsUrl({ name: "San Angel Inn", parkName: "EPCOT" });
    expect(apple).toContain("maps.apple.com/?q=San%20Angel%20Inn%20EPCOT");
    const goog = mapsUrl({ name: "San Angel Inn", parkName: "EPCOT", android: true });
    expect(goog).toContain("google.com/maps/search");
  });
});

describe("isAndroidUa", () => {
  it.each([
    ["Mozilla/5.0 (Linux; Android 15; Pixel 9) Chrome/126", true],
    ["Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X)", false],
    [null, false],
    [undefined, false],
  ])("%s → %s", (ua, expected) => {
    expect(isAndroidUa(ua)).toBe(expected);
  });
});
