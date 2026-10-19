"use client";

import { useCallback, useMemo, useState } from "react";

/**
 * Onboarding state machine. The same machine drives the /login wizard
 * (welcome → moodle → teams) and the /onboarding wizard (courses → prefs),
 * parameterized by which steps to surface and which
 * to count toward the progress bar.
 *
 * Resumable by design: there is no completion flag — the caller decides
 * which `initial` step to start from based on observed app state (which
 * platforms are connected, whether any courses are tracked).
 */
export type OnboardingStep =
  | "welcome"
  | "moodle"
  | "teams"
  | "courses"
  | "prefs"
  | "done";

export type OnboardingMachine = {
  step: OnboardingStep;
  /** 1-indexed position within the countable steps; 0 for welcome / done. */
  index: number;
  total: number;
  /** 1 forward, -1 back. Drives the slide animation in the stage. */
  direction: 1 | -1;
  next: () => void;
  back: () => void;
  skip: () => void;
  goTo: (step: OnboardingStep) => void;
};

export type OnboardingConfig = {
  /** Step to start the flow on. */
  initial: OnboardingStep;
  /**
   * Ordered list of steps the wizard walks through. Anything not in this
   * list is skipped when calling `next` / `back`. Must include `initial`.
   * Welcome and done are appended/prepended automatically when
   * `includeWelcome` is true.
   */
  steps: ReadonlyArray<OnboardingStep>;
  /** Whether to start with the welcome screen. Defaults to false. */
  includeWelcome?: boolean;
};

type Internal = { step: OnboardingStep; direction: 1 | -1 };

export function useOnboarding(config: OnboardingConfig): OnboardingMachine {
  const { initial, steps, includeWelcome } = config;

  const order = useMemo<OnboardingStep[]>(() => {
    const out: OnboardingStep[] = [];
    if (includeWelcome) out.push("welcome");
    for (const s of steps) {
      if (!out.includes(s)) out.push(s);
    }
    out.push("done");
    return out;
  }, [steps, includeWelcome]);

  const [internal, setInternal] = useState<Internal>(() => ({
    step: initial,
    direction: 1,
  }));

  const advance = useCallback(() => {
    setInternal(({ step }) => {
      const idx = order.indexOf(step);
      const nextStep = order[Math.min(idx + 1, order.length - 1)] as OnboardingStep;
      return { step: nextStep, direction: 1 };
    });
  }, [order]);

  const retreat = useCallback(() => {
    setInternal(({ step }) => {
      const idx = order.indexOf(step);
      const prevStep = order[Math.max(idx - 1, 0)] as OnboardingStep;
      return { step: prevStep, direction: -1 };
    });
  }, [order]);

  const goTo = useCallback(
    (target: OnboardingStep) => {
      setInternal(({ step }) => {
        const dir = order.indexOf(target) >= order.indexOf(step) ? 1 : -1;
        return { step: target, direction: dir };
      });
    },
    [order],
  );

  const countableIdx = steps.indexOf(internal.step);
  return {
    step: internal.step,
    index: countableIdx >= 0 ? countableIdx + 1 : 0,
    total: steps.length,
    direction: internal.direction,
    next: advance,
    back: retreat,
    skip: advance,
    goTo,
  };
}
