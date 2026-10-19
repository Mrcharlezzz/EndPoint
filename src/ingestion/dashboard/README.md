# Ingestor Dashboard

## Package manager

This project is pinned to `pnpm@11.0.8` via `package.json`.
Use Corepack so local installs, CI, and Docker resolve the same pnpm major:

```bash
corepack enable
pnpm install --frozen-lockfile
```

## Build-script approvals

The dashboard uses `@vitejs/plugin-react-swc`, which pulls in `@swc/core`.
With pnpm 11, dependency build scripts are blocked unless they are explicitly
reviewed. The reviewed allowlist lives in `pnpm-workspace.yaml`:

```yaml
allowBuilds:
  "@swc/core": true
```

If a future dependency adds an install script, review it and update
`pnpm-workspace.yaml` instead of disabling pnpm's build-script protections.
