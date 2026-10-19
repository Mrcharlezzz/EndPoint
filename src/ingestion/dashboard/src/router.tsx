import { createBrowserRouter, redirect } from 'react-router-dom';
import { LoginPage } from './routes/login';
import { ShellLayout } from './routes/shell';
import { OverviewPage } from './routes/overview';
import { UsersPage } from './routes/users';
import { UserDetailPage } from './routes/user-detail';
import { SyncsPage } from './routes/syncs';
import { ErrorsPage } from './routes/errors';
import { CredentialsPage } from './routes/credentials';
import { LogsPage } from './routes/logs';
import { AlertsPage } from './routes/alerts';
import { AuditPage } from './routes/audit';
import { ConfigPage } from './routes/config';
import { SettingsPage } from './routes/settings';
import { api } from './api/client';

async function requireAdmin(): Promise<Response | null> {
  const { response } = await api.GET('/api/auth/whoami', {});
  if (response.status === 401) {
    const url = new URL(window.location.href);
    return redirect(`/login?next=${encodeURIComponent(url.pathname + url.search)}`);
  }
  return null;
}

export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  {
    path: '/',
    element: <ShellLayout />,
    loader: requireAdmin,
    children: [
      { index: true, element: <OverviewPage /> },
      { path: 'users', element: <UsersPage /> },
      { path: 'users/:email', element: <UserDetailPage /> },
      { path: 'syncs', element: <SyncsPage /> },
      { path: 'errors', element: <ErrorsPage /> },
      { path: 'credentials', element: <CredentialsPage /> },
      { path: 'logs', element: <LogsPage /> },
      { path: 'alerts', element: <AlertsPage /> },
      { path: 'audit', element: <AuditPage /> },
      { path: 'config', element: <ConfigPage /> },
      { path: 'settings', element: <SettingsPage /> },
    ],
  },
]);
