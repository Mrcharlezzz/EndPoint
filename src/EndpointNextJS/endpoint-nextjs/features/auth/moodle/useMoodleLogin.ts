"use client";

import { useState } from "react";

import { moodleLogin } from "@/lib/api/auth";
import { log } from "@/lib/logger";
import { toAuthUser } from "@/lib/mappers/auth";

import { useAuthStore } from "../store";

type State = {
  submitting: boolean;
  error: string | null;
};

/**
 * Submits Moodle credentials, hydrates the auth store on success, and
 * surfaces submitting/error state for the caller. Navigation is left to
 * the caller (login wizard advances; settings re-attach stays on page).
 */
export function useMoodleLogin() {
  const [state, setState] = useState<State>({ submitting: false, error: null });

  async function submit(input: { username: string; password: string }): Promise<boolean> {
    setState({ submitting: true, error: null });
    const auth = useAuthStore.getState();
    auth.setStatus("authenticating");
    try {
      const { user } = await moodleLogin(input);
      auth.setUser(toAuthUser(user));
      setState({ submitting: false, error: null });
      return true;
    } catch (err) {
      log.error("auth.moodle.login.failed", err);
      const message = err instanceof Error ? err.message : "Sign-in failed.";
      auth.setStatus("error", message);
      setState({ submitting: false, error: message });
      return false;
    }
  }

  return { ...state, submit };
}
