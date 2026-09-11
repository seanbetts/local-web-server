import '@testing-library/jest-dom/vitest';

const values = new Map<string, string>();
const storage = {
  clear: () => values.clear(),
  getItem: (key: string) => values.get(key) ?? null,
  key: (index: number) => [...values.keys()][index] ?? null,
  get length() {
    return values.size;
  },
  removeItem: (key: string) => values.delete(key),
  setItem: (key: string, value: string) => values.set(key, value),
} satisfies Storage;
Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: storage,
});

if (!HTMLElement.prototype.getAnimations) {
  HTMLElement.prototype.getAnimations = () => [];
}
