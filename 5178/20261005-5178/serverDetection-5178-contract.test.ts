// Temporary contract test for phase-rs/phase#5178 re-validation (2026-10-05).
// Cases 1-2 are the PR's own published new cases (verbatim); cases 3-6 are
// controls from the prior backfill contract runs. REMOVED FROM THE WORKTREE
// AFTER THE RUN; this copy is preserved as evidence.
import { describe, expect, it, vi } from "vitest";
import { canUseLanBridge } from "../lan";
vi.mock("../lan", async (importOriginal) => ({
  ...((await importOriginal<typeof import("../lan")>()) as object),
  canUseLanBridge: vi.fn(() => false),
}));
import { parseJoinCode } from "../serverDetection";

describe("parseJoinCode — bracketed IPv6 host (#5178 contract)", () => {
  it("does not corrupt a bracketed IPv6 host that has no port", () => {
    // `[::1]` ends in `]`; its last colon is inside the address. Splitting there
    // produced a broken host `[:` and a malformed `wss://[::1/ws`.
    const r = parseJoinCode("ABC123@[::1]");
    expect(r.code).toBe("ABC123");
    expect(r.serverAddress).toBe("wss://[::1]/ws");
  });

  it("still splits a bracketed IPv6 host that DOES carry a port", () => {
    expect(parseJoinCode("ABC123@[::1]:9000").serverAddress).toBe(
      "wss://[::1]:9000/ws",
    );
  });

  it("handles a longer port-less bracketed IPv6 literal", () => {
    expect(parseJoinCode("ABC123@[2001:db8::1]").serverAddress).toBe(
      "wss://[2001:db8::1]/ws",
    );
  });

  it("hostname control resolves correctly", () => {
    expect(parseJoinCode("ABC123@play.example.com").serverAddress).toBe(
      "wss://play.example.com/ws",
    );
  });

  it("localhost control resolves to ws with default port", () => {
    expect(parseJoinCode("ABC123@localhost:9374").serverAddress).toBe(
      "ws://localhost:9374/ws",
    );
  });

  it("bare code control returns code only", () => {
    expect(parseJoinCode("ABC123")).toEqual({ code: "ABC123" });
  });
});
