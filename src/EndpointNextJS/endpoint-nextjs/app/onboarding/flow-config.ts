type OnboardingStep = "welcome" | "moodle" | "teams" | "courses" | "prefs" | "done";

type OnboardingFlowConfig = {
  initial: OnboardingStep;
  steps: ReadonlyArray<OnboardingStep>;
  includeWelcome?: boolean;
};

const FULL_STEPS: ReadonlyArray<OnboardingStep> = ["moodle", "teams", "courses", "prefs"];

const POST_AUTH_STEPS: ReadonlyArray<OnboardingStep> = ["courses", "prefs"];

type FlowConfigInput = {
  isAuthed: boolean;
  forceFullFlow: boolean;
  moodleConnected: boolean;
  teamsConnected: boolean;
  hasTrackedCourses: boolean;
};

export function getOnboardingFlowConfig({
  isAuthed,
  forceFullFlow,
  moodleConnected,
  teamsConnected,
  hasTrackedCourses,
}: FlowConfigInput): OnboardingFlowConfig {
  const fullFlow = !isAuthed || forceFullFlow;
  const steps = fullFlow ? FULL_STEPS : POST_AUTH_STEPS;
  const includeWelcome = fullFlow;

  const initial: OnboardingStep = (() => {
    if (fullFlow) {
      if (!isAuthed) return "welcome";
      if (!moodleConnected) return "moodle";
      if (!teamsConnected) return "teams";
      if (!hasTrackedCourses) return "courses";
      return "prefs";
    }
    if (!hasTrackedCourses) return "courses";
    return "prefs";
  })();

  return {
    initial,
    steps,
    includeWelcome,
  };
}
