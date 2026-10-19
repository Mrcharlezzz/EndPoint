import assert from "node:assert/strict";
import test from "node:test";

import { getOnboardingFlowConfig } from "./flow-config";

test("full flow skips telegram and moves from teams to courses", () => {
  const config = getOnboardingFlowConfig({
    isAuthed: false,
    forceFullFlow: false,
    moodleConnected: false,
    teamsConnected: false,
    hasTrackedCourses: false,
  });

  assert.deepEqual(config.steps, ["moodle", "teams", "courses", "prefs"]);
  assert.equal(config.initial, "welcome");
  assert.equal(config.includeWelcome, true);
});

test("returning users without tracked courses resume at courses", () => {
  const config = getOnboardingFlowConfig({
    isAuthed: true,
    forceFullFlow: false,
    moodleConnected: true,
    teamsConnected: true,
    hasTrackedCourses: false,
  });

  assert.deepEqual(config.steps, ["courses", "prefs"]);
  assert.equal(config.initial, "courses");
  assert.equal(config.includeWelcome, false);
});
