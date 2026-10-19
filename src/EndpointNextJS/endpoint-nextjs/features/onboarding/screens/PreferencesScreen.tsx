"use client";

import type { ApiPreferences } from "@/lib/api/types";
import { usePreferencesContext } from "@/features/preferences/PreferencesContext";

type Option<V extends string> = { value: V; label: string };

type RowProps<K extends keyof ApiPreferences> = {
  label: string;
  sub?: string;
  current: ApiPreferences[K] | undefined;
  options: ReadonlyArray<Option<NonNullable<ApiPreferences[K]> & string>>;
  onChange: (value: ApiPreferences[K]) => void;
};

function PrefRow<K extends keyof ApiPreferences>({
  label,
  sub,
  current,
  options,
  onChange,
}: RowProps<K>) {
  return (
    <div className="pref-row">
      <div>
        <div className="pref-row-label">{label}</div>
        {sub ? <div className="pref-row-sub">{sub}</div> : null}
      </div>
      <div className="pref-segmented">
        {options.map((o) => (
          <button
            key={o.value}
            type="button"
            data-on={current === o.value}
            onClick={() => onChange(o.value as ApiPreferences[K])}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

type Props = {
  onFinishAction: () => void;
  onBackAction: () => void;
  onSkipAction: () => void;
};

export function PreferencesScreen({ onFinishAction, onBackAction, onSkipAction }: Props) {
  const { prefs, setPref } = usePreferencesContext();

  return (
    <div className="onboarding-card onboarding-card--narrow">
      <h1 className="onboarding-title">Set your preferences</h1>
      <p className="onboarding-blurb">
        A few small choices to make Endpoint feel like yours. You can change all
        of these later from Settings.
      </p>

      <div className="prefs-list">
        <PrefRow<"landing">
          label="Default landing view"
          sub="Where Endpoint opens by default"
          current={prefs.landing}
          options={[
            { value: "assignments", label: "Assignments" },
            { value: "announcements", label: "Announcements" },
          ]}
          onChange={(v) => setPref("landing", v)}
        />

        <PrefRow<"theme">
          label="Theme"
          current={prefs.theme}
          options={[
            { value: "light", label: "Light" },
            { value: "dark", label: "Dark" },
            { value: "system", label: "System" },
          ]}
          onChange={(v) => setPref("theme", v)}
        />

        <PrefRow<"density">
          label="Density"
          sub="Information density of dashboard cards"
          current={prefs.density}
          options={[
            { value: "comfortable", label: "Comfortable" },
            { value: "standard", label: "Standard" },
            { value: "compact", label: "Compact" },
          ]}
          onChange={(v) => setPref("density", v)}
        />

        <PrefRow<"timeFmt">
          label="Time format"
          current={prefs.timeFmt}
          options={[
            { value: "12h", label: "12-hour" },
            { value: "24h", label: "24-hour" },
          ]}
          onChange={(v) => setPref("timeFmt", v)}
        />
      </div>

      <div className="onboarding-actions">
        <button type="button" className="btn-ghost" onClick={onBackAction}>
          ← Back
        </button>
        <button type="button" className="btn-secondary" onClick={onSkipAction}>
          Skip for now
        </button>
        <button type="button" className="btn-primary btn-large" onClick={onFinishAction}>
          Finish setup ✓
        </button>
      </div>
    </div>
  );
}
