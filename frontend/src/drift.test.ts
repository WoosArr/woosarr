import { describe, expect, it } from "vitest";
import { driftTone } from "./drift";

describe("driftTone", () => {
  it("is green under 30 s, yellow from 30 to 60 s, red over 60 s, either direction", () => {
    expect(driftTone(0)[0]).toBe("ok");
    expect(driftTone(29.9)[0]).toBe("ok");
    expect(driftTone(30)[0]).toBe("warn");
    expect(driftTone(-38)).toEqual(["warn", "Check NTP"]);
    expect(driftTone(60)[0]).toBe("warn");
    expect(driftTone(60.1)[0]).toBe("danger");
    expect(driftTone(-90)).toEqual(["danger", "Fix the clock"]);
  });
});
