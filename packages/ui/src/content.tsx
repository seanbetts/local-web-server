import type { ComponentPropsWithoutRef, ReactNode } from 'react';

const classes = (platformClass: string, className?: string) =>
  [platformClass, className].filter(Boolean).join(' ');

export interface ViewHeaderProps
  extends Omit<ComponentPropsWithoutRef<'header'>, 'title'> {
  title: ReactNode;
  eyebrow?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}

export function ViewHeader({
  title,
  eyebrow,
  description,
  actions,
  className,
  ...props
}: ViewHeaderProps) {
  return (
    <header className={classes('lwp-view-header', className)} {...props}>
      <div className="lwp-view-header__copy">
        {eyebrow ? <div className="lwp-view-header__eyebrow">{eyebrow}</div> : null}
        <h1>{title}</h1>
        {description ? (
          <div className="lwp-view-header__description">{description}</div>
        ) : null}
      </div>
      {actions ? <div className="lwp-view-header__actions">{actions}</div> : null}
    </header>
  );
}

export interface SectionNavProps
  extends Omit<ComponentPropsWithoutRef<'nav'>, 'aria-label'> {
  label: string;
}

export function SectionNav({ label, children, className, ...props }: SectionNavProps) {
  return (
    <nav
      className={classes('lwp-section-nav', className)}
      {...props}
      aria-label={label}
    >
      <div className="lwp-section-nav__viewport">{children}</div>
    </nav>
  );
}

export type MetricGroupProps = ComponentPropsWithoutRef<'dl'>;

export function MetricGroup({ className, ...props }: MetricGroupProps) {
  return <dl className={classes('lwp-metric-group', className)} {...props} />;
}

export interface MetricProps extends ComponentPropsWithoutRef<'div'> {
  label: ReactNode;
  value: ReactNode;
  detail?: ReactNode;
}

export function Metric({ label, value, detail, className, ...props }: MetricProps) {
  return (
    <div className={classes('lwp-metric', className)} {...props}>
      <dt>{label}</dt>
      <dd>
        <span className="lwp-metric__value">{value}</span>
        {detail ? <span className="lwp-metric__detail">{detail}</span> : null}
      </dd>
    </div>
  );
}

export interface DataViewportProps
  extends Omit<ComponentPropsWithoutRef<'div'>, 'aria-label' | 'role' | 'tabIndex'> {
  label: string;
}

export function DataViewport({ label, className, ...props }: DataViewportProps) {
  return (
    <div
      aria-label={label}
      className={classes('lwp-data-viewport', className)}
      role="region"
      tabIndex={0}
      {...props}
    />
  );
}
