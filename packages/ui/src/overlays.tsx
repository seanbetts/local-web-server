import {
  Children,
  cloneElement,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type DialogHTMLAttributes,
  type FocusEvent, type KeyboardEvent, type MouseEvent,
  type ReactElement,
  type ReactNode,
} from 'react';
import { IconButton } from './actions.js';
import { focusableElements, focusFirst } from './focus.js';
const classes = (...values: Array<string | undefined>) => values.filter(Boolean).join(' ');
const cssLengthInPixels = (value: string) => {
  const trimmed = value.trim();
  const amount = Number.parseFloat(trimmed);
  if (!Number.isFinite(amount)) return 0;
  if (trimmed.endsWith('px')) return amount;
  if (!trimmed.endsWith('rem')) return 0;
  const rootSize = Number.parseFloat(window.getComputedStyle(document.documentElement).fontSize);
  return Number.isFinite(rootSize) ? amount * rootSize : 0;
};
export type DialogProps = Omit<
  DialogHTMLAttributes<HTMLDialogElement>,
  'aria-label' | 'aria-labelledby' | 'onClose' | 'open' | 'title'
> & {
  actions?: ReactNode;
  description?: string;
  onClose: () => void;
  open: boolean;
  title: ReactNode;
};
export function Dialog({
  actions,
  children,
  className,
  description,
  onCancel,
  onClick,
  onClose,
  onKeyDown,
  open,
  title,
  ...props
}: DialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const wasOpenRef = useRef(false);
  const generatedTitleId = useId();
  const generatedDescriptionId = useId();
  const titleId = `lwp-dialog-title-${generatedTitleId}`;
  const descriptionId = description ? `lwp-dialog-description-${generatedDescriptionId}` : undefined;
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      if (!wasOpenRef.current && typeof HTMLElement !== 'undefined') {
        returnFocusRef.current = document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      }
      if (!dialog.open) {
        try {
          if (typeof dialog.showModal === 'function') dialog.showModal();
          else dialog.setAttribute('open', '');
        } catch {
          dialog.setAttribute('open', '');
        }
      }
      const activeElement = document.activeElement instanceof HTMLElement
        && dialog.contains(document.activeElement)
        ? document.activeElement
        : null;
      const body = dialog.querySelector<HTMLElement>('.lwp-dialog__body');
      focusFirst(
        dialog.querySelectorAll<HTMLElement>('[autofocus], [data-lwp-autofocus="true"]'),
        body ? focusableElements(body) : [],
        activeElement ? [activeElement] : [],
        focusableElements(dialog),
      );
    } else if (wasOpenRef.current) {
      if (dialog.open && typeof dialog.close === 'function') dialog.close();
      else dialog.removeAttribute('open');
      returnFocusRef.current?.focus();
      returnFocusRef.current = null;
    }
    wasOpenRef.current = open;
  }, [open]);
  useEffect(() => () => returnFocusRef.current?.focus(), []);
  const requestClose = () => onClose();
  const handleClick = (event: MouseEvent<HTMLDialogElement>) => {
    onClick?.(event);
    if (!event.defaultPrevented && event.target === event.currentTarget) requestClose();
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLDialogElement>) => {
    onKeyDown?.(event);
    if (!event.defaultPrevented && event.key === 'Escape') {
      event.preventDefault();
      requestClose();
    }
  };
  return (
    <dialog
      {...props}
      ref={dialogRef}
      className={classes('lwp-dialog', className)}
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      onCancel={(event) => {
        onCancel?.(event);
        if (event.defaultPrevented) return;
        event.preventDefault();
        requestClose();
      }}
      onClick={handleClick}
      onClose={() => {
        if (open) requestClose();
      }}
      onKeyDown={handleKeyDown}
    >
      <div className="lwp-dialog__surface">
        <header className="lwp-dialog__header">
          <h2 id={titleId}>{title}</h2>
          <IconButton label="Close" icon="x" onClick={requestClose} />
        </header>
        {description ? <p id={descriptionId}>{description}</p> : null}
        <div className="lwp-dialog__body">{children}</div>
        {actions ? <footer className="lwp-dialog__actions">{actions}</footer> : null}
      </div>
    </dialog>
  );
}
type TooltipTriggerProps = {
  'aria-describedby'?: string;
  onKeyDown?: (event: KeyboardEvent<HTMLElement>) => void;
};
export type TooltipProps = {
  children: ReactElement<TooltipTriggerProps>;
  className?: string;
  content: ReactNode;
};
export function Tooltip({ children, className, content }: TooltipProps) {
  const containerRef = useRef<HTMLSpanElement>(null);
  const contentRef = useRef<HTMLSpanElement>(null);
  const shiftRef = useRef(0);
  const [dismissed, setDismissed] = useState(false);
  const [focused, setFocused] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [shift, setShift] = useState(0);
  const tooltipId = `lwp-tooltip-${useId()}`;
  const trigger = Children.only(children);
  const description = [trigger.props['aria-describedby'], tooltipId].filter(Boolean).join(' ');
  const handleBlur = (event: FocusEvent<HTMLSpanElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget)) {
      setFocused(false);
      if (!hovered) setDismissed(false);
    }
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    trigger.props.onKeyDown?.(event);
    if (event.defaultPrevented || event.key !== 'Escape' || !visible) return;
    event.preventDefault();
    event.stopPropagation();
    setDismissed(true);
  };
  const visible = (focused || hovered) && !dismissed;
  useLayoutEffect(() => {
    if (!visible) {
      shiftRef.current = 0;
      setShift(0);
      return;
    }
    const container = containerRef.current;
    const tooltip = contentRef.current;
    if (!container || !tooltip || typeof window === 'undefined') return;
    const measure = () => {
      const rectangle = tooltip.getBoundingClientRect();
      const gutter = cssLengthInPixels(
        window.getComputedStyle(container).getPropertyValue('--lwp-space-2'),
      );
      const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
      const availableWidth = Math.max(0, viewportWidth - (gutter * 2));
      const unshiftedLeft = rectangle.left - shiftRef.current;
      const unshiftedRight = rectangle.right - shiftRef.current;
      let nextShift = 0;
      if (rectangle.width >= availableWidth || unshiftedLeft < gutter) {
        nextShift = gutter - unshiftedLeft;
      } else if (unshiftedRight > viewportWidth - gutter) {
        nextShift = viewportWidth - gutter - unshiftedRight;
      }
      nextShift = Math.round(nextShift * 100) / 100;
      shiftRef.current = nextShift;
      setShift((current) => current === nextShift ? current : nextShift);
    };
    measure();
    window.addEventListener('resize', measure);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(container);
    observer?.observe(tooltip);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', measure);
    };
  }, [content, visible]);
  const shiftStyle = shift === 0
    ? undefined
    : ({ '--lwp-tooltip-shift': `${shift}px` } as CSSProperties);
  return (
    <span
      ref={containerRef}
      className={classes('lwp-tooltip', className)}
      onBlur={handleBlur}
      onFocus={() => {
        if (!focused && !hovered) setDismissed(false);
        setFocused(true);
      }}
      onMouseEnter={() => {
        if (!focused && !hovered) setDismissed(false);
        setHovered(true);
      }}
      onMouseLeave={() => {
        setHovered(false);
        if (!focused) setDismissed(false);
      }}
    >
      {cloneElement(trigger, {
        'aria-describedby': description,
        onKeyDown: handleKeyDown,
      })}
      <span
        ref={contentRef}
        className="lwp-tooltip__content"
        id={tooltipId}
        role="tooltip"
        hidden={!visible}
        style={shiftStyle}
      >
        {content}
      </span>
    </span>
  );
}
