"use client";

import type { ApiPreferences } from "@/lib/api/types";
import { usePreferencesContext } from "@/features/preferences/PreferencesContext";

type Option<V extends string> = { value: V; label: string };

type RowProps<K extends keyof ApiPreferences> = {
  label: string;
  sub?: string;
  prefKey: K;
  current: ApiPreferences[K] | undefined;
  options: ReadonlyArray<Option<NonNullable<ApiPreferences[K]> & string>>;
  onChange: (value: ApiPreferences[K]) => void;
};

function Row<K extends keyof ApiPreferences>({
  label,
  sub,
  current,
  options,
  onChange,
}: RowProps<K>) {
  return (
    <div className="settings-row">
      <div>
        <div className="settings-row-label">{label}</div>
        {sub ? <div className="settings-row-sub">{sub}</div> : null}
      </div>
      <div className="segmented">
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

export function PreferencesTab() {
  const { prefs, setPref } = usePreferencesContext();

  return (
    <>
      <h3 className="settings-section-title">Preferences</h3>
      <p className="settings-section-sub">How Endpoint looks and behaves.</p>

      <Row<"landing">
        label="Default landing view"
        sub="Where Endpoint opens by default"
        prefKey="landing"
        current={prefs.landing}
        options={[
          { value: "assignments", label: "Assignments" },
          { value: "announcements", label: "Announcements" },
        ]}
        onChange={(v) => setPref("landing", v)}
      />

      <Row<"theme">
        label="Theme"
        sub="Top-bar toggle switches between light and dark"
        prefKey="theme"
        current={prefs.theme}
        options={[
          { value: "light", label: "Light" },
          { value: "dark", label: "Dark" },
          { value: "system", label: "System" },
        ]}
        onChange={(v) => setPref("theme", v)}
      />

      <Row<"density">
        label="Density"
        sub="Card spacing and information density"
        prefKey="density"
        current={prefs.density}
        options={[
          { value: "comfortable", label: "Comfortable" },
          { value: "standard", label: "Standard" },
          { value: "compact", label: "Compact" },
        ]}
        onChange={(v) => setPref("density", v)}
      />

      <Row<"accent">
        label="Accent"
        sub="Highlight color for buttons and links"
        prefKey="accent"
        current={prefs.accent}
        options={[
          { value: "blue", label: "Blue" },
          { value: "teal", label: "Teal" },
          { value: "slate", label: "Slate" },
        ]}
        onChange={(v) => setPref("accent", v)}
      />

      <Row<"timeFmt">
        label="Time format"
        prefKey="timeFmt"
        current={prefs.timeFmt}
        options={[
          { value: "12h", label: "12-hour" },
          { value: "24h", label: "24-hour" },
        ]}
        onChange={(v) => setPref("timeFmt", v)}
      />
    </>
  );
}
