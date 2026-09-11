import {
  forwardRef,
  type ButtonHTMLAttributes,
  type MouseEvent,
} from 'react';

import { Icon, type PlatformIconName } from './Icon.js';

export type ButtonVariant = 'primary' | 'secondary' | 'danger';

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  busy?: boolean;
  variant?: ButtonVariant;
};

const classes = (...values: Array<string | undefined>) => values.filter(Boolean).join(' ');

const isAriaDisabled = (value: ButtonProps['aria-disabled']) =>
  value === true || value === 'true';

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    'aria-disabled': ariaDisabled,
    busy = false,
    children,
    className,
    onClick,
    type = 'button',
    variant = 'secondary',
    ...props
  },
  ref,
) {
  const unavailable = busy || isAriaDisabled(ariaDisabled);
  const handleClick = (event: MouseEvent<HTMLButtonElement>) => {
    if (unavailable) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    onClick?.(event);
  };

  return (
    <button
      {...props}
      ref={ref}
      type={type}
      className={classes('lwp-button', `lwp-button--${variant}`, className)}
      aria-busy={busy || undefined}
      aria-disabled={busy ? true : ariaDisabled}
      onClick={handleClick}
    >
      {busy ? <Icon className="lwp-button__busy-icon" name="loader" /> : null}
      {children}
    </button>
  );
});

export type IconButtonProps = Omit<ButtonProps, 'aria-label' | 'children'> & {
  icon: PlatformIconName;
  label: string;
};

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { busy = false, className, icon, label, ...props },
  ref,
) {
  return (
    <Button
      {...props}
      ref={ref}
      busy={busy}
      className={classes('lwp-icon-button', className)}
      aria-label={label}
    >
      {busy ? null : <Icon name={icon} />}
    </Button>
  );
});
