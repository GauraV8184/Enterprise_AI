import { Sparkles } from "lucide-react";

import { Spinner } from "./Spinner";

export function SplashScreen() {
  return (
    <div className="splash-screen">
      <div className="splash-brand">
        <span className="brand-mark brand-mark-lg" aria-hidden="true">
          <Sparkles size={26} />
        </span>
        <div>
          <strong>Enterprise AI</strong>
          <span>Intelligence Platform</span>
        </div>
      </div>
      <div className="splash-loader">
        <Spinner size={20} />
        <span>Preparing your workspace</span>
      </div>
    </div>
  );
}
