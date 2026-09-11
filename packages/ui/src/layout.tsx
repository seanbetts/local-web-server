import type { ComponentPropsWithoutRef } from 'react';

export type LayoutProps = ComponentPropsWithoutRef<'div'>;

const classes = (platformClass: string, className?: string) =>
  [platformClass, className].filter(Boolean).join(' ');

export function Stack({ className, ...props }: LayoutProps) {
  return <div className={classes('lwp-stack', className)} {...props} />;
}

export function Cluster({ className, ...props }: LayoutProps) {
  return <div className={classes('lwp-cluster', className)} {...props} />;
}

export function Grid({ className, ...props }: LayoutProps) {
  return <div className={classes('lwp-grid', className)} {...props} />;
}

export function Surface({ className, ...props }: LayoutProps) {
  return <div className={classes('lwp-surface lwp-surface--primitive', className)} {...props} />;
}
