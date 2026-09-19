"use strict";

const { spawnSync } = require("node:child_process");

const result = spawnSync(
  "python3",
  ["-m", "unittest", "discover", "-v", "-p", "test_*.py"],
  { stdio: "inherit" },
);

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
