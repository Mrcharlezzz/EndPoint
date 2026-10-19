import type { ReactElement } from 'react';
import { useProcessorSummary, useResetProcessorWindow } from '@/api/queries';
import { useRelativeTime } from '@/hooks/useRelativeTime';
import { getErrorMessage } from '@/lib/errors';
import { toast } from '@/lib/toasts';
import { Button, Card, CardBody, CardHeader, Kpi } from '@/components/ui';
import { LiveChip } from '@/components/LiveChip';

export function ProcessorSummaryPanel(): ReactElement {
  const summary = useProcessorSummary();
  const resetWindow = useResetProcessorWindow();
  const data = summary.data;
  const lastProcessed = useRelativeTime(data?.health.last_processed_at ?? null);
  const baseline = useRelativeTime(data?.baseline_at ?? null);
  const windowStart = useRelativeTime(data?.window_start ?? null);

  async function onReset(): Promise<void> {
    try {
      await resetWindow.mutateAsync({ reason: 'reset processor dashboard window' });
      toast.success('Processor window reset', 'Dashboard counters now follow the new baseline.');
    } catch (err: unknown) {
      toast.error('Reset failed', getErrorMessage(err));
    }
  }

  return (
    <Card className="processor-panel">
      <CardHeader
        title={<LiveChip />}
        right={
          <Button size="sm" onClick={() => void onReset()} disabled={resetWindow.isPending}>
            {resetWindow.isPending ? 'Resetting…' : 'Reset window'}
          </Button>
        }
      >
        <h2 className="title processor-panel-title">Processor</h2>
      </CardHeader>
      <CardBody className="processor-panel-body">
        <div className="processor-meta">
          <span>
            effective window:{' '}
            <span className="processor-meta-value">{data?.window_start ? windowStart : '—'}</span>
          </span>
          <span>
            baseline:{' '}
            <span className="processor-meta-value">{data?.baseline_at ? baseline : 'rolling 24h'}</span>
          </span>
          <span>
            last processed:{' '}
            <span className="processor-meta-value">
              {data?.health.last_processed_at ? lastProcessed : '—'}
            </span>
          </span>
        </div>

        <div className="processor-metrics-grid">
          <div data-testid="processor-kpi-processed">
            <Kpi label="Processed" value={data?.health.processed_count ?? 0} size="small" />
          </div>
          <div data-testid="processor-kpi-failed">
            <Kpi
              label="Failed"
              value={data?.health.failed_count ?? 0}
              size="small"
              className="processor-kpi-err"
            />
          </div>
          <div data-testid="processor-kpi-pending">
            <Kpi label="Pending" value={data?.health.pending_queue ?? 0} size="small" />
          </div>
          <div data-testid="processor-kpi-last-processed">
            <Kpi
              label="Last processed"
              value={data?.health.last_processed_at ? lastProcessed : '—'}
              size="small"
            />
          </div>
          <div data-testid="processor-kpi-llm-calls">
            <Kpi label="LLM calls" value={data?.llm.calls ?? 0} size="small" />
          </div>
          <div data-testid="processor-kpi-input-tokens">
            <Kpi label="Input tokens" value={data?.llm.input_tokens ?? 0} size="small" />
          </div>
          <div data-testid="processor-kpi-output-tokens">
            <Kpi label="Output tokens" value={data?.llm.output_tokens ?? 0} size="small" />
          </div>
          <div data-testid="processor-kpi-llm-failed">
            <Kpi label="LLM failed" value={data?.llm.failed_count ?? 0} size="small" />
          </div>
          <div data-testid="processor-kpi-unclassified">
            <Kpi label="Unclassified" value={data?.llm.unclassified_count ?? 0} size="small" />
          </div>
        </div>

        {(summary.isLoading || summary.isError) && (
          <div className="processor-state tiny">
            {summary.isLoading && <span className="dim">Loading processor summary…</span>}
            {summary.isError && <span className="text-err">{getErrorMessage(summary.error)}</span>}
          </div>
        )}
      </CardBody>
    </Card>
  );
}
