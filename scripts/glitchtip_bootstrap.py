"""Idempotent GlitchTip dev/CI bootstrap.

Creates a fixed admin user + organization + project + project key so the
DSN is deterministic across every cold boot of the Docker Compose stack.
Lets teammates clone the repo and run ``docker compose up`` with the
same DSN already committed in ``.env.example`` — no UI clicks.

Run inside the GlitchTip container:
    docker compose run --rm glitchtip-bootstrap

Safe to re-run: every step uses ``get_or_create`` and keeps existing
rows untouched if they already match. The script exits 0 on success
and prints the resulting DSN so ``docker compose logs`` makes it
immediately copyable if anyone wants to verify.

Env vars (all required — set in .env via GLITCHTIP_BOOTSTRAP_*):
    BOOTSTRAP_ADMIN_EMAIL         admin user's email
    BOOTSTRAP_ADMIN_PASSWORD      admin user's password (dev-only!)
    BOOTSTRAP_ORG_NAME            organization display name
    BOOTSTRAP_ORG_SLUG            organization slug
    BOOTSTRAP_PROJECT_NAME        project display name
    BOOTSTRAP_PROJECT_SLUG        project slug
    BOOTSTRAP_PROJECT_PUBLIC_KEY  UUID used as the DSN public key
                                  (stays stable across cold boots)
    BOOTSTRAP_DOMAIN              public-facing GlitchTip URL, for DSN
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

# Match GlitchTip's own manage.py so ``django.setup()`` finds the settings
# module even when invoked outside the usual ``manage.py`` entrypoint.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from allauth.account.models import EmailAddress  # noqa: E402

from apps.organizations_ext.models import (  # noqa: E402
    Organization,
    OrganizationOwner,
    OrganizationUser,
    OrganizationUserRole,
)
from apps.projects.models import Project, ProjectKey  # noqa: E402
from apps.users.models import User  # noqa: E402


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
    org_slug = _required("BOOTSTRAP_ORG_SLUG")
    project_name = _required("BOOTSTRAP_PROJECT_NAME")
    project_slug = _required("BOOTSTRAP_PROJECT_SLUG")
    project_public_key = _required("BOOTSTRAP_PROJECT_PUBLIC_KEY")
    domain = _required("BOOTSTRAP_DOMAIN")

    with transaction.atomic():
        user, user_created = User.objects.get_or_create(
            email=admin_email,
            defaults={
                "is_active": True,
                "is_staff": True,
                "is_superuser": True,
                "name": "endpoint-admin",
            },
        )
        if user_created:
            user.set_password(admin_password)
            user.save(update_fields=["password"])

        # django-allauth gates actions (invites, project creation, etc.) on
        # a verified primary EmailAddress. Signup through the UI creates
        # this row automatically; the ORM path has to do it explicitly or
        # the sidebar buttons render disabled.
        EmailAddress.objects.update_or_create(
            user=user,
            email=admin_email,
            defaults={"verified": True, "primary": True},
        )

        org, _org_created = Organization.objects.get_or_create(
            slug=org_slug, defaults={"name": org_name}
        )

        ou, _ou_created = OrganizationUser.objects.get_or_create(
            user=user,
            organization=org,
            defaults={"role": OrganizationUserRole.OWNER},
        )
        if ou.role != OrganizationUserRole.OWNER:
            ou.role = OrganizationUserRole.OWNER
            ou.save(update_fields=["role"])

        OrganizationOwner.objects.get_or_create(
            organization=org, defaults={"organization_user": ou}
        )

        project, _project_created = Project.objects.get_or_create(
            organization=org,
            slug=project_slug,
            defaults={"name": project_name, "platform": "python"},
        )

        ProjectKey.objects.get_or_create(
            project=project,
            public_key=project_public_key,
            defaults={"is_active": True},
        )

    # DSN format: http://<public_key_hex_no_dashes>@<host>/<project_id>
    parsed = urlparse(domain)
    if not parsed.scheme or not parsed.netloc:
        print(f"error: BOOTSTRAP_DOMAIN must be a full URL, got {domain!r}", file=sys.stderr)
        return 2
    public_key_hex = project_public_key.replace("-", "")
    dsn = f"{parsed.scheme}://{public_key_hex}@{parsed.netloc}/{project.id}"

    print("glitchtip-bootstrap: ready")
    print(f"  admin email : {admin_email}")
    print(f"  organization: {org_name} (slug={org_slug})")
    print(f"  project     : {project_name} (id={project.id})")
    print(f"  DSN         : {dsn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
