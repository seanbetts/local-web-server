const focusableSelector = [
  'button',
  'input:not([type="hidden"])',
  'select',
  'textarea',
  'a[href]',
  'area[href]',
  '[contenteditable]:not([contenteditable="false"])',
  '[tabindex]',
  'summary',
].join(', ');

const canAttemptFocus = (element: HTMLElement) => (
  element.matches(focusableSelector)
  && !element.matches(':disabled')
  && !element.hidden
);

export const focusFirst = (...groups: Array<Iterable<HTMLElement>>) => {
  const visited = new Set<HTMLElement>();
  for (const group of groups) {
    for (const candidate of group) {
      if (visited.has(candidate) || !canAttemptFocus(candidate)) continue;
      visited.add(candidate);
      candidate.focus({ preventScroll: true });
      if (document.activeElement === candidate) return;
    }
  }
};

export const focusableElements = (container: ParentNode) => (
  container.querySelectorAll<HTMLElement>(focusableSelector)
);
