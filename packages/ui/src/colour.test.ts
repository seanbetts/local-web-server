import fixtures from '../../../platform_assets/accent-fixtures.json';
import { describe, expect, it } from 'vitest';

import { deriveAccessibleAccent } from './colour';

describe('deriveAccessibleAccent', () => {
  it('matches every shared Python fixture byte for byte', () => {
    for (const fixture of fixtures) {
      expect(
        deriveAccessibleAccent(fixture.seed, fixture.surface),
        `${fixture.seed} on ${fixture.surface}`,
      ).toBe(fixture.expected);
    }
  });

  it('rejects non-canonical colours without echoing their value', () => {
    for (const invalid of ['red', '#fff', '#abcdef', '#GG0000', ' #000000']) {
      expect(() => deriveAccessibleAccent(invalid, '#FFFFFF')).toThrow(
        'invalid colour',
      );
      try {
        deriveAccessibleAccent(invalid, '#FFFFFF');
      } catch (error) {
        expect(String(error)).not.toContain(invalid);
      }
    }
  });
});
