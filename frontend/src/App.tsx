import { lazy, useEffect } from "react";
import {
  BrowserRouter,
  Link,
  Outlet,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { ArrowLeft, Compass } from "lucide-react";

import { AuthProvider } from "./auth/AuthProvider";
import { AuthScreen } from "./auth/AuthScreen";
import { useAuth } from "./auth/useAuth";
import { AppErrorBoundary } from "./components/AppErrorBoundary";
import { AppShell } from "./components/AppShell";
import { SplashScreen } from "./components/SplashScreen";

const ChatPage = lazy(() =>
  import("./pages/ChatPage").then((module) => ({ default: module.ChatPage })),
);
const DashboardPage = lazy(() =>
  import("./pages/DashboardPage").then((module) => ({
    default: module.DashboardPage,
  })),
);
const DocumentsPage = lazy(() =>
  import("./pages/DocumentsPage").then((module) => ({
    default: module.DocumentsPage,
  })),
);
const ReportsPage = lazy(() =>
  import("./pages/ReportsPage").then((module) => ({
    default: module.ReportsPage,
  })),
);
const SettingsPage = lazy(() =>
  import("./pages/SettingsPage").then((module) => ({
    default: module.SettingsPage,
  })),
);
const WorkflowPage = lazy(() =>
  import("./pages/WorkflowPage").then((module) => ({
    default: module.WorkflowPage,
  })),
);

function AuthGate() {
  const { loading, user } = useAuth();

  if (loading) {
    return <SplashScreen />;
  }
  if (!user) {
    return <AuthScreen />;
  }
  return <Outlet />;
}

function ScrollToTop() {
  const { pathname } = useLocation();

  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  }, [pathname]);

  return null;
}

function NotFoundPage() {
  return (
    <div className="page not-found-page">
      <div className="not-found-card">
        <span className="not-found-icon"><Compass size={28} /></span>
        <p className="eyebrow">404</p>
        <h1>That page is not in the map</h1>
        <p>The route may have moved, or the address may be incomplete.</p>
        <Link to="/" className="btn btn-primary">
          <ArrowLeft size={16} />
          Back to overview
        </Link>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <AppErrorBoundary>
      <AuthProvider>
        <BrowserRouter>
          <ScrollToTop />
          <Routes>
            <Route element={<AuthGate />}>
              <Route element={<AppShell />}>
                <Route index element={<DashboardPage />} />
                <Route path="chat" element={<ChatPage />} />
                <Route path="documents" element={<DocumentsPage />} />
                <Route path="ask" element={<WorkflowPage />} />
                <Route path="reports" element={<ReportsPage />} />
                <Route path="settings" element={<SettingsPage />} />
                <Route path="*" element={<NotFoundPage />} />
              </Route>
            </Route>
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </AppErrorBoundary>
  );
}
