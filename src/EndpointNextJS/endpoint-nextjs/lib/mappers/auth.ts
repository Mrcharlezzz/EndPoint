import type { AuthUser } from "@/domain/auth";

import type { AuthApiUser } from "@/lib/api/types";

export function toAuthUser(user: AuthApiUser): AuthUser {
  return {
    id: user.id,
    email: user.email,
    displayName: user.display_name,
    createdAt: user.created_at,
  };
}
