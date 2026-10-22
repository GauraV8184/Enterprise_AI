import { useCallback, useEffect, useState } from "react";
import type { HealthResponse } from "../api/types";
import type { ApiClient } from "../api/client";
import { errorMessage } from "../api/errors";

export type HealthState =
  | { status: "checking"; health: null; error: null }
  | { status: "online"; health: HealthResponse; error: null }
  | { status: "offline"; health: null; error: string };

const POLL_INTERVAL_MS = 60_000;

export function useApiHealth(api: ApiClient) {
  const [state, setState] = useState<HealthState>({
    status: "checking",
    health: null,
    error: null,
  });
  const [refreshToken, setRefreshToken] = useState(0);

  const refresh = useCallback(() => {
    setRefreshToken((value) => value + 1);
  }, []);

  useEffect(() => {
    let active = true;
    setState((current) =>
      current.status === "checking"
        ? current
        : { status: "checking", health: null, error: null },
    );

    void api
      .getHealth()
      .then((health) => {
        if (active) {
          setState({ status: "online", health, error: null });
        }
      })
      .catch((error: unknown) => {
        if (active) {
          setState({
            status: "offline",
            health: null,
            error: errorMessage(error),
          });
        }
      });

    return () => {
      active = false;
    };
  }, [api, refreshToken]);

  useEffect(() => {
    const timer = window.setInterval(refresh, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  return { ...state, refresh };
}
