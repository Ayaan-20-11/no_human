// Unit tests for the pure parts of packaging/linux-acceptance.mjs — the Lane-A
// driver that launches the INSTALLED Linux app under a throwaway HOME on the CI
// runner (docs/LINUX.md §4/§6).
// The driver itself only runs on Linux with an installed package; these pin the
// argument contract and the dummy-credential shape so a CI edit cannot silently
// point it at the wrong binary or hand the setup screen a value it rejects.
import assert from "node:assert/strict";
import test from "node:test";
import { DUMMY_TOKEN, expectedBoardUrl, parseArgs }
  from "../packaging/linux-acceptance.mjs";
import { validateToken } from "./tokenStore.mjs";

test("parseArgs requires --exe and --home, defaults --mode to setup", () => {
  assert.throws(() => parseArgs([]), /--exe/);
  assert.throws(() => parseArgs(["--exe", "/opt/no_human/no_human"]), /--home/);
  const a = parseArgs(["--exe", "/opt/no_human/no_human", "--home", "/tmp/h", "--out", "/tmp/o"]);
  assert.deepEqual(a, { exe: "/opt/no_human/no_human", home: "/tmp/h", out: "/tmp/o", mode: "setup" });
  assert.equal(parseArgs(["--exe", "x", "--home", "y", "--mode", "board"]).mode, "board");
  assert.throws(() => parseArgs(["--exe", "x", "--home", "y", "--mode", "nope"]), /--mode/);
  assert.throws(() => parseArgs(["--exe", "x", "--home", "y", "--bogus"]), /unrecognized/);
});

test("the dummy token passes the setup screen's own validator and can never be a real credential", () => {
  // The same function token.html calls before saving — if its rules move, this
  // fails here instead of on a runner 20 minutes into a build.
  assert.equal(validateToken(DUMMY_TOKEN), "");
  assert.match(DUMMY_TOKEN, /^sk-ant-oat01-/);
  assert.match(DUMMY_TOKEN, /dummy-not-a-real-token$/);
  assert.doesNotMatch(DUMMY_TOKEN, /\s/);
});

test("the board URL is loopback on the configured port", () => {
  assert.equal(expectedBoardUrl(8420), "http://127.0.0.1:8420/");
});
