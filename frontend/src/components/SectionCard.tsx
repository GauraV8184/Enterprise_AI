import type { ReactNode } from "react";

interface SectionCardProps {
  title: string;
  description?: string;
  icon?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  id?: string;
}

export function SectionCard({
  title,
  description,
  icon,
  actions,
  children,
  className = "",
  id,
}: SectionCardProps) {
  return (
    <section className={`panel ${className}`.trim()} id={id}>
      <header className="panel-header">
        <div className="panel-title-wrap">
          {icon && <span className="panel-icon">{icon}</span>}
          <div>
            <h2>{title}</h2>
            {description && <p>{description}</p>}
          </div>
        </div>
        {actions && <div className="panel-actions">{actions}</div>}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}
