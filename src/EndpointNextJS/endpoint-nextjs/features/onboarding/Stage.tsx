"use client";

import type { ReactNode } from "react";

import { ThemeToggle } from "@/features/preferences/ThemeToggle";

type Props = {
  step: number;
  total: number;
  direction: 1 | -1;
  children: ReactNode;
  onExitAction?: () => void;
};

/**
 * Full-screen onboarding shell: brand + animated progress bar + step
 * label, slide-animated frame, footer dots. Step content renders inside
 * `.onboarding-screen` so each transition replays the slide animation.
 */
export function OnboardingStage({ step, total, direction, children, onExitAction }: Props) {
  const fillPct = total > 0 ? (Math.max(0, Math.min(step, total)) / total) * 100 : 0;
  const slideClass = direction === 1 ? "onboarding-screen--in-right" : "onboarding-screen--in-left";

  return (
    <div className="onboarding-stage" aria-label="Endpoint setup">
      <div className="onboarding-bg" aria-hidden />

      <header className="onboarding-header">
        <div className="onboarding-brand" aria-hidden>
          <span className="onboarding-brand-mark">
            <span />
          </span>
          <span>Endpoint</span>
        </div>

        <div className="onboarding-progress">
          <div className="onboarding-progress-track">
            <div className="onboarding-progress-fill" style={{ width: `${fillPct}%` }} />
          </div>
          {step > 0 ? (
            <span className="onboarding-step-label">
              Step {step} of {total}
            </span>
          ) : (
            <span className="onboarding-step-label">Welcome</span>
          )}
        </div>

        <div className="onboarding-header-actions">
          <ThemeToggle />
          {onExitAction ? (
            <button type="button" className="btn-ghost" onClick={onExitAction}>
              Exit setup
            </button>
          ) : null}
        </div>
      </header>

      <div className="onboarding-frame">
        <div key={step} className={`onboarding-screen ${slideClass}`}>
          {children}
        </div>
      </div>

      <footer className="onboarding-footer">
        <span className="onboarding-dots" aria-hidden>
          {Array.from({ length: total + 1 }).map((_, i) => (
            <span
              key={i}
              className="onboarding-dot"
              data-on={i === step}
              data-done={i < step}
            />
          ))}
        </span>
      </footer>
    </div>
  );
}
