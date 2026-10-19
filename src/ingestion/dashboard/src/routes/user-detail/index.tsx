import type { ReactElement } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import { useUser, useUserStats } from '@/api/queries';
import { PagePlaceholder } from '@/components/PagePlaceholder';
import { Header } from './Header';
import { TabBar } from './TabBar';
import { OverviewTab } from './OverviewTab';
import { CredentialsTab } from './CredentialsTab';
import { SyncHistoryTab } from './SyncHistoryTab';
import { ContributionsTab } from './ContributionsTab';
import { ErrorsTab } from './ErrorsTab';
import { AuditTab } from './AuditTab';
import { parseTab, type TabId } from './lib';
import '@/styles/pages/user-detail.css';

export function UserDetailPage(): ReactElement {
  const { email } = useParams<{ email: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = parseTab(searchParams.get('tab'));

  const q = useUser(email ?? '');
  const stats = useUserStats(email ?? '');

  if (email === undefined || email.length === 0) {
    return <PagePlaceholder title="User" subtitle="missing email" />;
  }
  if (q.isLoading) {
    return <PagePlaceholder title={email} subtitle="loading…" />;
  }
  if (q.isError || q.data === undefined) {
    return <PagePlaceholder title={email} subtitle="user not found" />;
  }

  const user = q.data;
  const contribCount = stats.data?.total_items;

  const onTab = (next: TabId) => {
    const next_ = new URLSearchParams(searchParams);
    if (next === 'overview') next_.delete('tab');
    else next_.set('tab', next);
    setSearchParams(next_, { replace: true });
  };

  return (
    <>
      <Header user={user} />
      <div className="ud-tabbar-wrap">
        <TabBar
          active={activeTab}
          onChange={onTab}
          contributionsCount={contribCount}
        />
      </div>
      <div className="ud-tabcontent">
        {activeTab === 'overview' && <OverviewTab user={user} />}
        {activeTab === 'credentials' && <CredentialsTab user={user} />}
        {activeTab === 'syncs' && <SyncHistoryTab user={user} />}
        {activeTab === 'contributions' && <ContributionsTab user={user} />}
        {activeTab === 'errors' && <ErrorsTab user={user} />}
        {activeTab === 'audit' && <AuditTab user={user} />}
      </div>
    </>
  );
}
