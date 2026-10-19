"use client";

import { useState } from "react";
import { Plug, RotateCw } from "lucide-react";

import { track } from "@/features/analytics/events";
import { MoodleLoginModal } from "@/features/auth/moodle/MoodleLoginModal";
import { TeamsLoginModal } from "@/features/auth/teams/TeamsLoginModal";
import type { ApiConnection, ApiPlatform } from "@/lib/api/types";
import {
  useConnections,
  useDisconnectPlatform,
} from "@/lib/queries/useConnections";

type ConnectablePlatform = "moodle" | "teams";

type Row = {
  platform: ApiPlatform;
  label: string;
  iconLetter: string;
};

const ROWS: ReadonlyArray<Row> = [
  { platform: "moodle", label: "Moodle", iconLetter: "M" },
  { platform: "teams", label: "Microsoft Teams", iconLetter: "T" },
];

function statusLabel(conn: ApiConnection | undefined): string {
  if (!conn) return "Not connected";
  if (conn.status === "connected") {
    return conn.last_sync_at
      ? `Connected · synced ${formatRel(conn.last_sync_at)}`
      : "Connected";
  }
  if (conn.status === "expired") return "Expired — reconnect to resume syncs";
  if (conn.status === "paused") return "Paused";
  return "Not connected";
}

function statusDot(conn: ApiConnection | undefined): "connected" | "expired" | "paused" | "off" {
  if (!conn) return "off";
  if (conn.status === "connected") return "connected";
  if (conn.status === "expired") return "expired";
  if (conn.status === "paused") return "paused";
  return "off";
}

function formatRel(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "recently";
  const delta = Date.now() - then;
  const m = Math.round(delta / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} hour${h === 1 ? "" : "s"} ago`;
  const d = Math.round(h / 24);
  return `${d} day${d === 1 ? "" : "s"} ago`;
}

export function PlatformsTab() {
  const { data, isLoading } = useConnections();
  const { mutate: disconnect } = useDisconnectPlatform();
  const [openModal, setOpenModal] = useState<ConnectablePlatform | null>(null);

  const byPlatform = new Map<string, ApiConnection>();
  (data ?? []).forEach((c) => byPlatform.set(c.platform, c));

  return (
    <>
      <h3 className="settings-section-title">Platforms</h3>
      <p className="settings-section-sub">
        Reconnect or disconnect your linked services.
      </p>

      {isLoading ? <div className="empty-hint">Loading connections…</div> : null}

      {!isLoading
        ? ROWS.map((row) => {
            const conn = byPlatform.get(row.platform);
            const isConnected = conn?.status === "connected";
            return (
              <div key={row.platform} className="platform-row">
                <div className="platform-logo" data-platform={row.platform} aria-hidden>
                  {row.iconLetter}
                </div>
                <div className="platform-info">
                  <div className="platform-name">{row.label}</div>
                  <div className="platform-status">
                    <span
                      className="status-dot"
                      data-status={statusDot(conn)}
                      aria-hidden
                    />
                    {statusLabel(conn)}
                  </div>
                </div>
                <div className="platform-actions">
                  {!isConnected ? (
                    <button
                      type="button"
                      className="btn-primary btn-sm"
                      onClick={() => setOpenModal(row.platform as ConnectablePlatform)}
                    >
                      <Plug size={13} aria-hidden />
                      Connect
                    </button>
                  ) : null}
                  {isConnected ? (
                    <button
                      type="button"
                      className="btn-secondary btn-sm"
                      onClick={() => setOpenModal(row.platform as ConnectablePlatform)}
                    >
                      <RotateCw size={13} aria-hidden />
                      Reconnect
                    </button>
                  ) : null}
                  {isConnected ? (
                    <button
                      type="button"
                      className="btn-text btn-sm"
                      onClick={() => {
                        const platform = row.platform as ConnectablePlatform;
                        disconnect(platform, {
                          onSuccess: () => track("connection.disconnected", { platform }),
                        });
                      }}
                    >
                      Disconnect
                    </button>
                  ) : null}
                </div>
              </div>
            );
          })
        : null}

      <MoodleLoginModal
        open={openModal === "moodle"}
        onSuccessAction={() => {
          track("connection.linked", { platform: "moodle" });
          setOpenModal(null);
        }}
        onCloseAction={() => setOpenModal(null)}
      />
      <TeamsLoginModal
        open={openModal === "teams"}
        onSuccessAction={() => {
          track("connection.linked", { platform: "teams" });
          setOpenModal(null);
        }}
        onCloseAction={() => setOpenModal(null)}
      />
    </>
  );
}
