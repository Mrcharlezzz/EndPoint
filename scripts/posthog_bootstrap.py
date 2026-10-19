"""Idempotent PostHog dev/CI bootstrap.

Mirrors ``scripts/glitchtip_bootstrap.py``: creates a fixed admin user,
organization, and team with a deterministic ``api_token`` so the same
``phc_...`` key works across cold boots of the Docker Compose stack.

Lets teammates clone the repo and run ``docker compose up`` with the
project key already committed in ``.env.example`` — no UI clicks.

Run inside the PostHog container:
    docker compose run --rm posthog-bootstrap

Safe to re-run: every step uses ``get_or_create`` and reconciles existing
rows back to the bootstrap values if they've drifted.

Env vars (all required — set in .env via POSTHOG_BOOTSTRAP_*):
    BOOTSTRAP_ADMIN_EMAIL         admin user's email
    BOOTSTRAP_ADMIN_PASSWORD      admin user's password (dev-only!)
    BOOTSTRAP_ORG_NAME            organization display name
    BOOTSTRAP_PROJECT_NAME        project (Team) display name
    BOOTSTRAP_PROJECT_API_TOKEN   the ``phc_...`` key the client SDK uses
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "posthog.settings")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from posthog.models.organization import (  # noqa: E402
    Organization,
    OrganizationMembership,
)
from posthog.models.team import Team  # noqa: E402
from posthog.models.user import User  # noqa: E402


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"error: {name} is required", file=sys.stderr)
        sys.exit(2)
    return value


def main() -> int:
    admin_email = _required("BOOTSTRAP_ADMIN_EMAIL")
    admin_password = _required("BOOTSTRAP_ADMIN_PASSWORD")
    org_name = _required("BOOTSTRAP_ORG_NAME")
    project_name = _required("BOOTSTRAP_PROJECT_NAME")
    project_api_token = _required("BOOTSTRAP_PROJECT_API_TOKEN")

    with transaction.atomic():
        user, user_created = User.objects.get_or_create(
            email=admin_email,
            defaults={
                "first_name": "Endpoint",
                "is_active": True,
                "is_staff": True,
                "is_email_verified": True,
            },
        )
        if user_created:
            user.set_password(admin_password)
            user.save()

        org, _org_created = Organization.objects.get_or_create(name=org_name)

        OrganizationMembership.objects.get_or_create(
            user=user,
            organization=org,
            defaults={"level": OrganizationMembership.Level.OWNER},
        )

        team, _team_created = Team.objects.get_or_create(
            organization=org,
            name=project_name,
            defaults={"api_token": project_api_token},
        )
        # Reconcile if a previous run created the team with a different token.
        if team.api_token != project_api_token:
            team.api_token = project_api_token
            team.save(update_fields=["api_token"])

        # Bind the user's "current" pointers so the dashboard lands on
        # the bootstrapped project after first sign-in.
        if (
            user.current_organization_id != org.id
            or user.current_team_id != team.id
        ):
            user.current_organization = org
            user.current_team = team
            user.save(
                update_fields=["current_organization", "current_team"]
            )

    print("posthog-bootstrap: ready")
    print(f"  admin email      : {admin_email}")
    print(f"  organization     : {org_name}")
    print(f"  project          : {project_name} (id={team.id})")
    print(f"  project API token: {project_api_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
