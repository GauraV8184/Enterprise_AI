import { useEffect } from "react";

export function useDocumentTitle(title: string) {
  useEffect(() => {
    document.title = title
      ? `${title} · Enterprise AI`
      : "Enterprise AI Intelligence Platform";
  }, [title]);
}
