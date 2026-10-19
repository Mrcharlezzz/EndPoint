"use client";

import { useCallback } from "react";
import { useRouter } from "next/navigation";

import "@/features/auth/auth.css";

import { track } from "@/features/analytics/events";
import { MoodleLoginModal } from "@/features/auth/moodle/MoodleLoginModal";
import { TeamsLoginModal } from "@/features/auth/teams/TeamsLoginModal";
import { useOnboarding } from "@/features/onboarding/machine";
import { OnboardingStage } from "@/features/onboarding/Stage";
import { CoursesScreen } from "@/features/onboarding/screens/CoursesScreen";
import { PreferencesScreen } from "@/features/onboarding/screens/PreferencesScreen";
import { ConnectStep } from "@/features/onboarding/steps/ConnectStep";
import { WelcomeStep } from "@/features/onboarding/steps/WelcomeStep";
import { useConnections } from "@/lib/queries/useConnections";
import { useCourses } from "@/lib/queries/useCourses";

import { getOnboardingFlowConfig } from "./flow-config";

const MOODLE_PERMS = [
  "Course list and metadata",
  "Assignments and deadlines",
  "Announcement posts",
  "We will not post on your behalf",
];

const TEAMS_PERMS = [
  "Class teams and channel posts",
  "Assignments distributed via Teams",
  "Calendar events for your courses",
  "We will not post on your behalf",
];

type Props = {
  /** True when the request carried a session cookie. */
  isAuthed: boolean;
  /** True when the user clicked "Re-run setup" — walk the full flow. */
  forceFullFlow: boolean;
};

export function OnboardingFlow({ isAuthed, forceFullFlow }: Props) {
  const router = useRouter();
  const { data: connections } = useConnections();
  const { data: tracked } = useCourses();

  const moodleConnected = (connections ?? []).some(
    (c) => c.platform === "moodle" && c.status === "connected",
  );
  const teamsConnected = (connections ?? []).some(
    (c) => c.platform === "teams" && c.status === "connected",
  );
  const hasTrackedCourses = (tracked ?? []).length > 0;

  const flow = getOnboardingFlowConfig({
    isAuthed,
    forceFullFlow,
    moodleConnected,
    teamsConnected,
    hasTrackedCourses,
  });

  const machine = useOnboarding({
    initial: flow.initial,
    steps: flow.steps,
    includeWelcome: flow.includeWelcome,
  });
  const currentStep = machine.step;

  const trackedNext = useCallback(() => {
    track("onboarding.step_complete", { step: currentStep });
    machine.next();
  }, [currentStep, machine]);

  const trackedSkip = useCallback(() => {
    track("onboarding.step_skipped", { step: currentStep });
    machine.skip();
  }, [currentStep, machine]);

  const finish = () => {
    track("onboarding.finished", {});
    router.push("/assignments");
  };
  // The exit affordance only makes sense once the user has signed in;
  // an unauthed visitor on welcome has nothing to exit to.
  const exit = isAuthed ? () => router.push("/assignments") : undefined;

  return (
    <OnboardingStage
      step={machine.index}
      total={machine.total}
      direction={machine.direction}
      onExitAction={exit}
    >
      {machine.step === "welcome" ? (
        <WelcomeStep onContinueAction={trackedNext} />
      ) : null}

      {machine.step === "moodle" ? (
        <ConnectStep
          platform="moodle"
          iconLetter="M"
          title="Connect Moodle"
          blurb="We'll pull your assignments and announcements from Moodle. We never store your password — just an authentication token you can revoke anytime."
          perms={MOODLE_PERMS}
          renderModalAction={({ open, onSuccessAction, onCloseAction }) => (
            <MoodleLoginModal
              open={open}
              onSuccessAction={() => {
                track("connection.linked", { platform: "moodle" });
                onSuccessAction();
              }}
              onCloseAction={onCloseAction}
            />
          )}
          onContinueAction={trackedNext}
          onBackAction={machine.back}
        />
      ) : null}

      {machine.step === "teams" ? (
        <ConnectStep
          platform="teams"
          iconLetter="T"
          title="Connect Microsoft Teams"
          blurb="Endpoint pulls course channel posts and announcements from Teams. We open a real Microsoft sign-in screen so MFA, federation, and passkeys all work as you expect."
          perms={TEAMS_PERMS}
          renderModalAction={({ open, onSuccessAction, onCloseAction }) => (
            <TeamsLoginModal
              open={open}
              onSuccessAction={() => {
                track("connection.linked", { platform: "teams" });
                onSuccessAction();
              }}
              onCloseAction={onCloseAction}
            />
          )}
          onContinueAction={trackedNext}
          onBackAction={machine.back}
        />
      ) : null}

      {machine.step === "courses" ? (
        <CoursesScreen
          onContinueAction={trackedNext}
          onBackAction={machine.back}
          onSkipAction={trackedSkip}
        />
      ) : null}

      {machine.step === "prefs" ? (
        <PreferencesScreen
          onFinishAction={finish}
          onBackAction={machine.back}
          onSkipAction={finish}
        />
      ) : null}

      {machine.step === "done" ? (
        <div className="onboarding-card onboarding-card--narrow">
          <h1 className="onboarding-title">All set</h1>
          <p className="onboarding-blurb">Taking you to your dashboard…</p>
          <button type="button" className="btn-primary btn-large" onClick={finish}>
            Open Endpoint →
          </button>
        </div>
      ) : null}
    </OnboardingStage>
  );
}
