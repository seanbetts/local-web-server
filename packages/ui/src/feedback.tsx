import type { HTMLAttributes, ReactNode } from 'react';

import { Icon, type PlatformIconName } from './Icon.js';

export type FeedbackTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';

export type FeedbackStateProps = {
  action?: ReactNode;
  className?: string;
  message?: string;
  title: string;
};

export type BadgeProps = HTMLAttributes<HTMLSpanElement> & {
  tone?: Exclude<FeedbackTone, 'info'>;
};

const classes = (...values: Array<string | undefined>) => values.filter(Boolean).join(' ');

export function Badge({ className, tone = 'neutral', ...props }: BadgeProps) {
  return <span className={classes('lwp-badge', `lwp-tone--${tone}`, className)} {...props} />;
}

export type StatusDotProps = Omit<HTMLAttributes<HTMLSpanElement>, 'children'> & {
  label: string;
  tone: Exclude<FeedbackTone, 'info'>;
};

export function StatusDot({ className, label, tone, ...props }: StatusDotProps) {
  return (
    <span
      {...props}
      className={classes('lwp-status-dot', `lwp-tone--${tone}`, className)}
      role="status"
      aria-label={label}
      aria-live="polite"
    />
  );
}

export type InlineNoticeProps = Omit<HTMLAttributes<HTMLDivElement>, 'title'> &
  FeedbackStateProps & {
    tone?: 'info' | 'success' | 'warning' | 'error';
  };

export function InlineNotice({
  action,
  className,
  message,
  title,
  tone = 'info',
  ...props
}: InlineNoticeProps) {
  const isError = tone === 'error';
  return (
    <div
      {...props}
      className={classes(
        'lwp-inline-notice',
        `lwp-tone--${isError ? 'danger' : tone}`,
        className,
      )}
      role={isError ? 'alert' : 'status'}
      aria-label={title}
      aria-live={isError ? 'assertive' : 'polite'}
    >
      <strong>{title}</strong>
      {message ? <span>{message}</span> : null}
      {action ? <div className="lwp-feedback__action">{action}</div> : null}
    </div>
  );
}

type FeedbackProps = FeedbackStateProps & {
  busy?: boolean;
  icon: PlatformIconName;
  kind: 'loading' | 'empty' | 'error';
};

function Feedback({ action, busy, className, icon, kind, message, title }: FeedbackProps) {
  const isError = kind === 'error';
  return (
    <div
      className={classes('lwp-feedback', `lwp-feedback--${kind}`, className)}
      role={isError ? 'alert' : 'status'}
      aria-label={title}
      aria-live={isError ? 'assertive' : 'polite'}
      aria-busy={busy || undefined}
    >
      <Icon className="lwp-feedback__icon" name={icon} />
      <div className="lwp-feedback__copy">
        <strong>{title}</strong>
        {message ? <span>{message}</span> : null}
      </div>
      {action ? <div className="lwp-feedback__action">{action}</div> : null}
    </div>
  );
}

export function LoadingState(props: FeedbackStateProps) {
  return <Feedback {...props} busy icon="loader" kind="loading" />;
}

export function EmptyState(props: FeedbackStateProps) {
  return <Feedback {...props} icon="map-pin" kind="empty" />;
}

export function ErrorState(props: FeedbackStateProps) {
  return <Feedback {...props} icon="circle-alert" kind="error" />;
}
