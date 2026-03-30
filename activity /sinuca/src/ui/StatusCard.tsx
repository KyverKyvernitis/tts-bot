import type { PropsWithChildren } from "react";

interface StatusCardProps extends PropsWithChildren {
  title: string;
  subtitle?: string;
}

export default function StatusCard({ title, subtitle, children }: StatusCardProps) {
  return (
    <section className="status-card">
      <div className="status-card__head">
        <h2>{title}</h2>
        {subtitle ? <p>{subtitle}</p> : null}
      </div>
      <div className="status-card__body">{children}</div>
    </section>
  );
}
