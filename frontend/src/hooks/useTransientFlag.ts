import { useEffect, useRef, useState } from "react";

/**
 * True for `duration` ms whenever `trigger` becomes a new, non-empty value.
 *
 * Used to flash a freshly generated answer. The flag clears itself so the
 * highlight is a one-off cue and never becomes a permanent visual state.
 */
export function useTransientFlag(trigger: unknown, duration = 2400): boolean {
  const [active, setActive] = useState(false);
  const previous = useRef<unknown>(undefined);

  useEffect(() => {
    if (Object.is(previous.current, trigger)) {
      return;
    }
    previous.current = trigger;

    if (trigger === null || trigger === undefined || trigger === false) {
      setActive(false);
      return;
    }

    setActive(true);
    const timer = window.setTimeout(() => setActive(false), duration);
    return () => window.clearTimeout(timer);
  }, [trigger, duration]);

  return active;
}
