import { createContext } from "react";

import type { ApiClient } from "../api/client";

export type AuthMode = "supabase" | "development" | "misconfigured";

export interface AuthUser {
  id: string;
  email: string | null;
  name: string;
}

export interface SignUpResult {
  needsConfirmation: boolean;
}

export interface AuthContextValue {
  mode: AuthMode;
  user: AuthUser | null;
  loading: boolean;
  sessionError: string | null;
  api: ApiClient;
  devUserId: string;
  setDevUserId: (value: string) => void;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (email: string, password: string) => Promise<SignUpResult>;
  signOut: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue | null>(null);
