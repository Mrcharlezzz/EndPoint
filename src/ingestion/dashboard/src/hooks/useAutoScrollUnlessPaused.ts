import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';

/**
 * Auto-scroll a container to the top when new rows arrive, UNLESS the
 * user has scrolled away from the top (indicating they want to keep
 * their position). Returns `{ ref, paused, resume }` — render a "resume"
 * button when `paused` is true.
 *
 * Assumes newest rows are at the top (scrollTop = 0). Flip the logic if
 * your feed grows from the bottom.
 */
export function useAutoScrollUnlessPaused<T extends HTMLElement>(
  dep: unknown,
  threshold = 16,
): { ref: RefObject<T | null>; paused: boolean; resume: () => void } {
  const ref = useRef<T | null>(null);
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    function onScroll(): void {
      if (!el) return;
      setPaused(el.scrollTop > threshold);
    }
    el.addEventListener('scroll', onScroll);
    return () => el.removeEventListener('scroll', onScroll);
  }, [threshold]);

  useEffect(() => {
    if (paused) return;
    const el = ref.current;
    if (!el) return;
    el.scrollTop = 0;
  }, [dep, paused]);

  const resume = useCallback(() => {
    setPaused(false);
    const el = ref.current;
    if (el) el.scrollTop = 0;
  }, []);

  return { ref, paused, resume };
}
