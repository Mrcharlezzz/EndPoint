"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowUpRight, Download, Eraser } from "lucide-react";

import { track } from "@/features/analytics/events";
import { clearReadState } from "@/lib/api/readState";
import { queryKeys } from "@/lib/queries/keys";

import { ConfirmDialog } from "./ConfirmDialog";

export function DataTab() {
  const router = useRouter();
  const qc = useQueryClient();
  const [confirmClear, setConfirmClear] = useState(false);

  const { mutate: clear, isPending: clearing } = useMutation<void, unknown, void>({
    mutationFn: clearReadState,
    onSuccess: () => {
      qc.setQueryData(queryKeys.readState(), {});
      track("read_history.cleared", {});
    },
    meta: { errorToast: "Couldn't clear read history." },
  });

  return (
    <>
      <h3 className="settings-section-title">Data</h3>
      <p className="settings-section-sub">
        Manage data Endpoint stores about you.
      </p>

      <div className="settings-row">
        <div>
          <div className="settings-row-label">Re-run setup</div>
          <div className="settings-row-sub">
            Walk through onboarding again — connect platforms, pick groups and
            courses.
          </div>
        </div>
        <button
          type="button"
          className="btn-secondary btn-sm"
          onClick={() => router.push("/onboarding?from=welcome")}
        >
          Open setup
          <ArrowUpRight size={13} aria-hidden />
        </button>
      </div>

      <div className="settings-row">
        <div>
          <div className="settings-row-label">Read history</div>
          <div className="settings-row-sub">
            Forget which items you&apos;ve marked as read. Your data on Moodle,
            Teams, and Telegram is unaffected.
          </div>
        </div>
        <button
          type="button"
          className="btn-secondary btn-secondary--danger btn-sm"
          onClick={() => setConfirmClear(true)}
        >
          <Eraser size={13} aria-hidden />
          Clear history
        </button>
      </div>

      <div className="settings-row">
        <div>
          <div className="settings-row-label">Export data</div>
          <div className="settings-row-sub">Download a copy of everything Endpoint has stored. Coming soon.</div>
        </div>
        <button type="button" className="btn-secondary btn-sm" disabled>
          <Download size={13} aria-hidden />
          Export
        </button>
      </div>

      {confirmClear ? (
        <ConfirmDialog
          title="Clear read history?"
          body="This forgets which items you've already seen. The items themselves stay on Moodle, Teams, and Telegram."
          confirmLabel={clearing ? "Clearing…" : "Clear history"}
          danger
          confirmDisabled={clearing}
          onCancelAction={() => setConfirmClear(false)}
          onConfirmAction={() =>
            clear(undefined, { onSettled: () => setConfirmClear(false) })
          }
        />
      ) : null}
    </>
  );
}
