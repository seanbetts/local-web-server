import { readFile, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const outputPath = resolve(repositoryRoot, 'platform_assets', 'icons.json');
const identityPath = resolve(repositoryRoot, 'platform_assets', 'app-identities.json');

const identityCatalogue = JSON.parse(await readFile(identityPath, 'utf8'));
const manifestDefinitions = Object.freeze(identityCatalogue.icons);
const manifest = Object.freeze(manifestDefinitions.map(({ name }) => name));
const actions = Object.freeze([
  'arrow-down',
  'arrow-up',
  'camera',
  'car',
  'caravan',
  'check',
  'chevron-down',
  'chevron-left',
  'chevron-right',
  'chevron-up',
  'circle-alert',
  'copy',
  'download',
  'grip-vertical',
  'image',
  'loader',
  'map-pin',
  'minus',
  'paperclip',
  'pencil',
  'plus',
  'refresh',
  'search',
  'ship',
  'signpost',
  'trash',
  'truck',
  'x',
]);
const internal = Object.freeze(['external-link']);

const sourceNames = Object.freeze({
  ...Object.fromEntries(manifestDefinitions.map(({ name, source }) => [name, source])),
  'circle-alert': 'alert-circle',
  image: 'photo',
  signpost: 'direction-sign',
});

const componentNames = Object.freeze({
  'weather-sun': 'IconSunHigh',
  'circle-alert': 'IconAlertCircle',
  image: 'IconPhoto',
  signpost: 'IconDirectionSign',
});

const pascalCase = (name) =>
  name
    .split('-')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join('');

const escapeAttribute = (value) =>
  String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('"', '&quot;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');

async function buildCatalogue() {
  const reactEntry = fileURLToPath(import.meta.resolve('@tabler/icons-react'));
  const reactPackageRoot = resolve(dirname(reactEntry), '..', '..');
  const reactPackage = JSON.parse(
    await readFile(resolve(reactPackageRoot, 'package.json'), 'utf8'),
  );
  const iconPackageRoot = resolve(reactPackageRoot, '..', 'icons');
  const iconPackage = JSON.parse(
    await readFile(resolve(iconPackageRoot, 'package.json'), 'utf8'),
  );

  if (reactPackage.dependencies?.['@tabler/icons'] !== iconPackage.version) {
    throw new Error('Tabler React and geometry package versions differ');
  }

  const nodes = JSON.parse(
    await readFile(resolve(iconPackageRoot, 'tabler-nodes-outline.json'), 'utf8'),
  );
  const icons = {};
  for (const name of [...manifest, ...actions, ...internal].sort()) {
    const source = sourceNames[name] ?? name;
    const iconNodes = nodes[source];
    if (!Array.isArray(iconNodes) || iconNodes.length === 0) {
      throw new Error(`Tabler icon is unavailable: ${source}`);
    }
    if (iconNodes.some(([tag]) => tag !== 'path')) {
      throw new Error(`Tabler icon uses unsupported geometry: ${source}`);
    }

    icons[name] = {
      component: componentNames[name] ?? `Icon${pascalCase(source)}`,
      source,
      svg: iconNodes
        .map(([tag, attributes]) => {
          const serialized = Object.entries(attributes)
            .sort(([first], [second]) => first.localeCompare(second))
            .map(([key, value]) => `${key}="${escapeAttribute(value)}"`)
            .join(' ');
          return `<${tag} ${serialized}/>`;
        })
        .join(''),
    };
  }

  return `${JSON.stringify(
    {
      package: '@tabler/icons-react',
      version: reactPackage.version,
      manifest,
      actions,
      internal,
      icons,
    },
    null,
    2,
  )}\n`;
}

const content = await buildCatalogue();
if (process.argv.includes('--check')) {
  let current;
  try {
    current = await readFile(outputPath, 'utf8');
  } catch {
    throw new Error('generated icon catalogue is missing');
  }
  if (current !== content) {
    throw new Error('generated icon catalogue is stale');
  }
} else {
  await writeFile(outputPath, content, 'utf8');
}
