import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  createClient,
  type Session,
  type SupabaseClient,
} from "@supabase/supabase-js";

import { createApiClient } from "../api/client";
import { ApiError } from "../api/errors";
import {
  DEFAULT_DEV_USER_ID,
  SUPABASE_ANON_KEY,
  SUPABASE_CONFIGURED,
  SUPABASE_MISCONFIGURED,
  SUPABASE_URL,
} from "../lib/config";
import {
  AuthContext,
  type AuthContextValue,
  type AuthMode,
  type AuthUser,
  type SignUpResult,
} from "./context";

const DEV_USER_STORAGE_KEY = "enterprise-ai.dev-user-id";
const MAX_USER_ID_LENGTH = 128;

function readStoredDevUserId(): string {
  try {
    return localStorage.getItem(DEV_USER_STORAGE_KEY) ?? DEFAULT_DEV_USER_ID;
  } catch {
    return DEFAULT_DEV_USER_ID;
  }
}

function resolveMode(): AuthMode {
  if (SUPABASE_CONFIGURED) {
    return "supabase";
  }
  if (SUPABASE_MISCONFIGURED) {
    return "misconfigured";
  }
  return "development";
}

function displayName(session: Session): string {
  const metadata = session.user.user_metadata as Record<string, unknown>;
  const candidate =
    (typeof metadata.full_name === "string" && metadata.full_name) ||
    (typeof metadata.name === "string" && metadata.name) ||
    session.user.email;
  return candidate || "Signed-in user";
}

function friendlyAuthMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  return "Authentication failed. Please try again.";
}

/**
 * One client for the lifetime of the page.
 *
 * Creating it inside a component or state initializer makes React StrictMode
 * construct a second client during its development double-render, which
 * triggers Supabase's "Multiple GoTrueClient instances" warning.
 */
const supabase: SupabaseClient | null = SUPABASE_CONFIGURED
  ? createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
        flowType: "pkce",
      },
    })
  : null;

export function AuthProvider({ children }: { children: ReactNode }) {
  const mode = useMemo(() => resolveMode(), []);

  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(Boolean(supabase));
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [devUserId, setDevUserIdState] = useState(readStoredDevUserId);

  useEffect(() => {
    if (!supabase) {
      return;
    }

    let active = true;
    void supabase.auth
      .getSession()
      .then(({ data, error }) => {
        if (!active) {
          return;
        }
        if (error) {
          setSessionError(friendlyAuthMessage(error));
        }
        setSession(data.session);
        setLoading(false);
      })
      .catch((error: unknown) => {
        if (active) {
          setSessionError(friendlyAuthMessage(error));
          setLoading(false);
        }
      });

    const { data: subscription } = supabase.auth.onAuthStateChange(
      (_event, nextSession) => {
        if (!active) {
          return;
        }
        setSession(nextSession);
        setSessionError(null);
        setLoading(false);
      },
    );

    return () => {
      active = false;
      subscription.subscription.unsubscribe();
    };
  }, []);

  const setDevUserId = useCallback((value: string) => {
    const trimmed = value.trim();
    if (!trimmed) {
      throw new Error("The development user id cannot be empty.");
    }
    if (trimmed.length > MAX_USER_ID_LENGTH) {
      throw new Error(
        `The development user id must be at most ${MAX_USER_ID_LENGTH} characters.`,
      );
    }
    try {
      localStorage.setItem(DEV_USER_STORAGE_KEY, trimmed);
    } catch {
      // Storage can be unavailable in private modes; the in-memory identity
      // still works for this tab.
    }
    setDevUserIdState(trimmed);
  }, []);

  const getAuthHeaders = useCallback(async (): Promise<
    Record<string, string>
  > => {
    if (mode === "development") {
      return { "X-User-Id": devUserId };
    }
    if (mode === "misconfigured" || !supabase) {
      throw new ApiError(
        503,
        "auth_not_configured",
        "Supabase Auth is only half configured. Set both VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY.",
      );
    }

    const { data, error } = await supabase.auth.getSession();
    if (error) {
      throw new ApiError(401, "invalid_token", friendlyAuthMessage(error));
    }

    let current = data.session;
    if (
      current?.expires_at &&
      current.expires_at * 1000 < Date.now() + 30_000
    ) {
      const refreshed = await supabase.auth.refreshSession();
      if (!refreshed.error) {
        current = refreshed.data.session;
      }
    }

    if (!current?.access_token) {
      throw new ApiError(
        401,
        "not_authenticated",
        "Your session has expired. Sign in again.",
      );
    }

    return { Authorization: `Bearer ${current.access_token}` };
  }, [devUserId, mode]);

  const api = useMemo(
    () => createApiClient(getAuthHeaders),
    [getAuthHeaders],
  );

  const signIn = useCallback(
    async (email: string, password: string) => {
      if (!supabase) {
        throw new Error("Supabase Auth is not configured.");
      }
      const { error } = await supabase.auth.signInWithPassword({
        email,
        password,
      });
      if (error) {
        throw new Error(friendlyAuthMessage(error));
      }
    },
    [],
  );

  const signUp = useCallback(
    async (email: string, password: string): Promise<SignUpResult> => {
      if (!supabase) {
        throw new Error("Supabase Auth is not configured.");
      }
      const { data, error } = await supabase.auth.signUp({
        email,
        password,
      });
      if (error) {
        throw new Error(friendlyAuthMessage(error));
      }
      return { needsConfirmation: !data.session };
    },
    [],
  );

  const signOut = useCallback(async () => {
    if (!supabase) {
      return;
    }
    const { error } = await supabase.auth.signOut();
    if (error) {
      throw new Error(friendlyAuthMessage(error));
    }
    setSession(null);
  }, []);

  const user = useMemo<AuthUser | null>(() => {
    if (mode === "development") {
      return {
        id: devUserId,
        email: null,
        name: "Development identity",
      };
    }
    if (!session) {
      return null;
    }
    return {
      id: session.user.id,
      email: session.user.email ?? null,
      name: displayName(session),
    };
  }, [devUserId, mode, session]);

  const value = useMemo<AuthContextValue>(
    () => ({
      mode,
      user,
      loading,
      sessionError,
      api,
      devUserId,
      setDevUserId,
      signIn,
      signUp,
      signOut,
    }),
    [
      api,
      devUserId,
      loading,
      mode,
      sessionError,
      setDevUserId,
      signIn,
      signOut,
      signUp,
      user,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
