const CANONICAL_HEX = /^#[0-9A-F]{6}$/;

type Channels = readonly [number, number, number];

const channels = (value: string): Channels => {
  if (!CANONICAL_HEX.test(value)) {
    throw new Error('invalid colour');
  }
  return [1, 3, 5].map((index) => Number.parseInt(value.slice(index, index + 2), 16)) as [
    number,
    number,
    number,
  ];
};

const hex = (value: Channels): string =>
  `#${value.map((channel) => channel.toString(16).padStart(2, '0')).join('')}`.toUpperCase();

const linear = (channel: number): number => {
  const value = channel / 255;
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
};

const luminance = (value: string): number => {
  const [red, green, blue] = channels(value);
  return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue);
};

const contrastRatio = (first: string, second: string): number => {
  const values = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
};

/** Port of the host's deterministic nearest-colour accessibility algorithm. */
export function deriveAccessibleAccent(seed: string, surface: string): string {
  const source = channels(seed);
  channels(surface);
  const candidates = new Map<string, Channels>();

  for (let step = 0; step <= 255; step += 1) {
    const blackward = source.map((channel) =>
      Math.round((channel * (255 - step)) / 255),
    ) as [number, number, number];
    const whiteward = source.map((channel) =>
      Math.round(channel + ((255 - channel) * step) / 255),
    ) as [number, number, number];
    candidates.set(hex(blackward), blackward);
    candidates.set(hex(whiteward), whiteward);
  }

  const eligible = [...candidates.entries()]
    .filter(([value]) => contrastRatio(value, surface) >= 3)
    .map(([value, candidate]) => ({
      value,
      distance: candidate.reduce(
        (total, channel, index) => total + (channel - source[index]) ** 2,
        0,
      ),
    }))
    .sort(
      (first, second) =>
        first.distance - second.distance || first.value.localeCompare(second.value),
    );

  if (eligible.length === 0) {
    throw new Error('cannot derive accessible accent');
  }
  return eligible[0].value;
}
