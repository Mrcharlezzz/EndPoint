"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { LogOut, Trash2 } from "lucide-react";

import { useDeleteMe, useLogout, useMe } from "@/lib/queries/useMe";

import { ConfirmDialog } from "./ConfirmDialog";

export function AccountTab() {
  const router = useRouter();
  const { data: user, isLoading } = useMe();
  const { mutate: logout, isPending: loggingOut } = useLogout();
  const { mutate: deleteMe, isPending: deleting } = useDeleteMe();
  const [confirmDelete, setConfirmDelete] = useState(false);

  return (
    <>
      <h3 className="settings-section-title">Account</h3>
      <p className="settings-section-sub">Manage your Endpoint account.</p>

      <div className="settings-row">
        <div>
          <div className="settings-row-label">Email</div>
          <div className="settings-row-sub">
            {isLoading ? "Loading…" : user?.email ?? "—"}
          </div>
        </div>
      </div>

      <div className="settings-row">
        <div>
          <div className="settings-row-label">Display name</div>
          <div className="settings-row-sub">
            {isLoading ? "Loading…" : user?.display_name || "—"}
          </div>
        </div>
      </div>

      <div className="settings-row">
        <div>
          <div className="settings-row-label">Session</div>
          <div className="settings-row-sub">Sign out of Endpoint on this device.</div>
        </div>
        <button
          type="button"
          className="btn-text btn-sm"
          disabled={loggingOut}
          onClick={() =>
            logout(undefined, {
              onSuccess: () => router.push("/onboarding"),
            })
          }
        >
          <LogOut size={13} aria-hidden />
          {loggingOut ? "Signing out…" : "Sign out"}
        </button>
      </div>

      <div className="settings-row">
        <div>
          <div className="settings-row-label">Delete account</div>
          <div className="settings-row-sub">
            Removes your Endpoint account, connections, and read history.
            Your data on Moodle, Teams, and Telegram is untouched.
          </div>
        </div>
        <button
          type="button"
          className="btn-secondary btn-secondary--danger btn-sm"
          onClick={() => setConfirmDelete(true)}
        >
          <Trash2 size={13} aria-hidden />
          Delete…
        </button>
      </div>

      {confirmDelete ? (
        <ConfirmDialog
          title="Delete your account?"
          body="This removes your Endpoint account, your platform connections, and your read history. The data on Moodle, Teams, and Telegram is unaffected."
          confirmLabel={deleting ? "Deleting…" : "Delete account"}
          danger
          confirmDisabled={deleting}
          onCancelAction={() => setConfirmDelete(false)}
          onConfirmAction={() =>
            deleteMe(undefined, {
              onSuccess: () => router.push("/onboarding"),
              onSettled: () => setConfirmDelete(false),
            })
          }
        />
      ) : null}
    </>
  );
}
