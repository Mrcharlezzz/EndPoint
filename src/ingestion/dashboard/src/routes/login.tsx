import { useState, type FormEvent } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useLogin } from '@/api/queries';
import { getErrorMessage } from '@/lib/errors';

export function LoginPage() {
  const [password, setPassword] = useState('');
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const login = useLogin();

  async function onSubmit(e: FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    try {
      await login.mutateAsync({ password });
      const next = params.get('next') ?? '/';
      void navigate(next, { replace: true });
    } catch {
      // error surfaces via login.error below
    }
  }

  return (
    <main className="login-shell">
      <form
        className="login-card"
        onSubmit={(e) => {
          void onSubmit(e);
        }}
      >
        <div className="login-brand">
          <span className="login-dot" />
          <span>ingestor</span>
        </div>
        <label className="login-label" htmlFor="pw">
          admin password
        </label>
        <input
          id="pw"
          type="password"
          autoFocus
          autoComplete="current-password"
          className="login-input"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          disabled={login.isPending}
        />
        {login.isError && (
          <div className="login-error" role="alert">
            {getErrorMessage(login.error)}
          </div>
        )}
        <button
          type="submit"
          className="login-submit"
          disabled={login.isPending || password.length === 0}
        >
          {login.isPending ? 'signing in…' : 'sign in'}
        </button>
      </form>
    </main>
  );
}
