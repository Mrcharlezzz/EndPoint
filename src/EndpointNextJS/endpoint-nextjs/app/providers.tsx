"use client";

import { QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { useState } from "react";

import { PostHogProvider } from "@/features/analytics/PostHogProvider";
import { makeQueryClient } from "@/lib/api/queryClient";
import { ToastViewport, toast } from "@/ui/toast";

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() =>
    makeQueryClient({
      onQueryError: ({ isBackgroundRefetch, meta }) => {
        if (!isBackgroundRefetch || meta.errorToast === false) return;
        toast.error(typeof meta.errorToast === "string" ? meta.errorToast : "Couldn't refresh.");
      },
      onMutationError: ({ meta }) => {
        if (meta.errorToast === false) return;
        toast.error(typeof meta.errorToast === "string" ? meta.errorToast : "Something went wrong.");
      },
    }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <PostHogProvider>
        {children}
        <ToastViewport />
        {process.env.NODE_ENV === "development" ? (
          <ReactQueryDevtools initialIsOpen={false} />
        ) : null}
      </PostHogProvider>
    </QueryClientProvider>
  );
}
