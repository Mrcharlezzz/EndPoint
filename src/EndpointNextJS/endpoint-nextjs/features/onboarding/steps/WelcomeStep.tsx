"use client";

type Props = {
  onContinueAction: () => void;
};

export function WelcomeStep({ onContinueAction }: Props) {
  return (
    <div className="onboarding-card onboarding-card--narrow">
      <div className="onboarding-hero-mark" aria-hidden>
        <div className="hero-orbits">
          <span className="orbit orbit-1" />
          <span className="orbit orbit-2" />
          <span className="orbit orbit-3" />
          <span className="hero-dot" />
        </div>
      </div>

      <h1 className="onboarding-title">Welcome to Endpoint</h1>
      <p className="onboarding-tagline">Your courses, in one place.</p>
      <p className="onboarding-blurb">
        Endpoint pulls together everything you need to keep up with your courses
        — assignments, announcements, and events — from Moodle, Teams, and
        Telegram. No more switching between apps.
      </p>

      <button type="button" className="btn-primary btn-large" onClick={onContinueAction}>
        Get started →
      </button>
    </div>
  );
}
