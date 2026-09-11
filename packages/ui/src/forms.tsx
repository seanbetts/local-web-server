import {
  Children,
  cloneElement,
  forwardRef,
  useId,
  type AriaAttributes,
  type InputHTMLAttributes,
  type ReactElement,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react';

const classes = (...values: Array<string | undefined>) => values.filter(Boolean).join(' ');

type FieldControlProps = {
  id?: string;
  'aria-describedby'?: string;
  'aria-invalid'?: AriaAttributes['aria-invalid'];
};

export type FieldProps = {
  children: ReactElement<FieldControlProps>;
  className?: string;
  error?: ReactNode;
  hint?: ReactNode;
  label: ReactNode;
};

const describedBy = (...values: Array<string | undefined>) => {
  const identifiers = values.flatMap((value) => value?.split(/\s+/).filter(Boolean) ?? []);
  return identifiers.length > 0 ? [...new Set(identifiers)].join(' ') : undefined;
};

export function Field({ children, className, error, hint, label }: FieldProps) {
  const generatedId = `lwp-field-${useId()}`;
  const control = Children.only(children);
  const controlId = control.props.id ?? generatedId;
  const hasHint = hint !== undefined && hint !== null && hint !== '';
  const hasError = error !== undefined && error !== null && error !== '';
  const hintId = hasHint ? `${controlId}-hint` : undefined;
  const errorId = hasError ? `${controlId}-error` : undefined;

  return (
    <div className={classes('lwp-field', className)}>
      <label className="lwp-field__label" htmlFor={controlId}>{label}</label>
      {cloneElement(control, {
        id: controlId,
        'aria-describedby': describedBy(
          control.props['aria-describedby'],
          hintId,
          errorId,
        ),
        'aria-invalid': hasError ? true : control.props['aria-invalid'],
      })}
      {hasHint ? <div className="lwp-field__hint" id={hintId}>{hint}</div> : null}
      {hasError ? <div className="lwp-field__error" id={errorId}>{error}</div> : null}
    </div>
  );
}

export type TextInputProps = InputHTMLAttributes<HTMLInputElement>;

export const TextInput = forwardRef<HTMLInputElement, TextInputProps>(function TextInput(
  { autoFocus, className, type = 'text', ...props },
  ref,
) {
  return <input {...props} ref={ref} autoFocus={autoFocus} data-lwp-autofocus={autoFocus || undefined} type={type} className={classes('lwp-control lwp-input', className)} />;
});

export type TextAreaProps = TextareaHTMLAttributes<HTMLTextAreaElement>;

export const TextArea = forwardRef<HTMLTextAreaElement, TextAreaProps>(function TextArea(
  { autoFocus, className, ...props },
  ref,
) {
  return <textarea {...props} ref={ref} autoFocus={autoFocus} data-lwp-autofocus={autoFocus || undefined} className={classes('lwp-control lwp-textarea', className)} />;
});

export type SelectProps = SelectHTMLAttributes<HTMLSelectElement>;

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { autoFocus, className, ...props },
  ref,
) {
  return <select {...props} ref={ref} autoFocus={autoFocus} data-lwp-autofocus={autoFocus || undefined} className={classes('lwp-control lwp-select', className)} />;
});
