import { useEffect, useRef } from "react";

/** Keeps a textarea's height in step with its content, up to a CSS cap. */
export function useAutoResize(
  value: string,
  maxHeight = 210,
): React.RefObject<HTMLTextAreaElement | null> {
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element) {
      return;
    }
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, maxHeight)}px`;
  }, [maxHeight, value]);

  return ref;
}
