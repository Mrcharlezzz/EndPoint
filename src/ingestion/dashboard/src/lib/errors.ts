export function getErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'string') return error;
  if (
    error !== null &&
    typeof error === 'object' &&
    'detail' in error &&
    typeof (error as { detail: unknown }).detail === 'string'
  ) {
    return (error as { detail: string }).detail;
  }
  return 'Unexpected error';
}
