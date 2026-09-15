import { execFileSync } from 'node:child_process';
import console from 'node:console';
import { mkdir, mkdtemp, readFile, readdir, rm, stat, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import process from 'node:process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const sourcePackage = JSON.parse(await readFile(join(repositoryRoot, 'package.json'), 'utf8'));
const prepared = process.argv.length === 3 && process.argv[2] === '--prepared';
const suppliedTarball = process.argv.length === 4 && process.argv[2] === '--tarball'
  ? resolve(process.argv[3]) : undefined;
if (process.argv.length !== 2 && !prepared && !suppliedTarball) {
  throw new Error('usage: check_ui_consumer.mjs [--prepared | --tarball path]');
}
const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
const REQUIRED_OFFLINE_INSTALL_FLAGS = Object.freeze([
  '--offline',
  '--legacy-peer-deps',
  '--ignore-scripts',
  '--no-audit',
  '--no-fund',
  '--no-package-lock',
  '--no-save',
]);

function run(command, args, cwd, environment = process.env) {
  execFileSync(command, args, { cwd, env: environment, stdio: 'inherit' });
}

function createOfflineTarballInstallArgs(tarball) {
  return ['install', ...REQUIRED_OFFLINE_INSTALL_FLAGS, tarball];
}

function assertOfflineTarballInstall(args, tarball) {
  const expectedArgs = [
    'install',
    '--offline',
    '--legacy-peer-deps',
    '--ignore-scripts',
    '--no-audit',
    '--no-fund',
    '--no-package-lock',
    '--no-save',
    tarball,
  ];
  if (args.length !== expectedArgs.length || args.some((argument, index) => argument !== expectedArgs[index])) {
    throw new Error('consumer install may only resolve the packed local UI tarball');
  }
}

function assertOfflineInstallGuardRejectsTamperedArguments() {
  const tarball = '/local/local-web-ui-0.1.0.tgz';
  const missingOffline = createOfflineTarballInstallArgs(tarball).filter(
    (argument) => argument !== '--offline',
  );
  try {
    assertOfflineTarballInstall(missingOffline, tarball);
  } catch {
    return;
  }
  throw new Error('consumer install guard accepted an argv without --offline');
}

assertOfflineInstallGuardRejectsTamperedArguments();

const disposableRoot = await mkdtemp(join(tmpdir(), 'local web ui consumer-'));
const consumerRoot = join(disposableRoot, 'consumer');
const consumerCache = join(disposableRoot, 'npm-cache');
await mkdir(consumerRoot);
await mkdir(consumerCache);
const npmEnvironment = {
  ...process.env,
  NPM_CONFIG_CACHE: consumerCache,
  npm_config_cache: consumerCache,
};
const consumerDependencies = [
  'react',
  'react-dom',
  'vite',
  'typescript',
  '@types/react',
  '@types/react-dom',
  '@types/node',
];

try {
  if (!prepared && !suppliedTarball) {
    run(npm, ['run', 'build:ui'], repositoryRoot, npmEnvironment);
  }

  const packageDestination = join(consumerRoot, 'package');
  await mkdir(packageDestination);
  await writeFile(
    join(consumerRoot, 'package.json'),
    `${JSON.stringify(
      {
        name: 'local-web-ui-clean-consumer',
        version: '1.0.0',
        private: true,
        type: 'module',
        // Apps supply the declared peers and their real TypeScript declarations.
        // Link the exact locked root installations, never Local Web source aliases.
        devDependencies: Object.fromEntries(
          consumerDependencies.map((name) => [name, sourcePackage.devDependencies[name]]),
        ),
      },
      null,
      2,
    )}\n`,
  );
  await writeFile(
    join(consumerRoot, 'src.tsx'),
    [
      "import { AppShell, ContextExportButton, DataViewport, Icon, Metric, MetricGroup, PlatformShell, SectionNav, ViewHeader, readColourMode, renderContextExport, validateLocalWebContext, type AppIdentity, type ColourMode, type ContextExportBuilder, type LocalWebContextV1, type PlatformContentMode, type PlatformIconName, type PlatformLocation } from '@local-web/ui';",
      "import type { ReactNode } from 'react';",
      "import { defineInteractiveExportContract, mountInteractiveExport, type InteractiveExportDefinition } from '@local-web/ui';",
      "import { localWebInteractiveExport } from '@local-web/ui/vite';",
      "import descriptor from 'virtual:local-web-interactive-export';",
      '',
      "const selectedMode: ColourMode = 'dark';",
      "const villaApp: AppIdentity = {",
      "  id: 'villa-shirt-collection',",
      "  name: 'Sample Workspace',",
      "  icon: 'shirt-sport',",
      "  accent: '#7A1735',",
      "};",
      "const platformLocation: PlatformLocation = {",
      "  kind: 'app-page',",
      "  app: villaApp,",
      "  appHref: '/samplebeta/',",
      "  pageLabel: 'David Platt',",
      "};",
      "const platformContentMode: PlatformContentMode = 'edge-to-edge';",
      "const themeIconNames: PlatformIconName[] = ['device-desktop', 'sun', 'moon'];",
      "const legacyApp: AppIdentity = {",
      "  id: 'legacy-app',",
      "  name: 'Legacy app',",
      "  icon: 'apps',",
      "  accent: '#123456',",
      '};',
      "const contextExport: LocalWebContextV1 = {",
      "  schema: 'local-web-context/v1',",
      "  app: { id: 'consumer', name: 'Consumer', version: '1.0.0', sourceRevision: 'abc123' },",
      "  context: { title: 'Consumer context', scope: 'clean consumer', activeRoute: '/', generatedAt: '2026-08-22T00:00:00.000Z', observedAt: null, dataRevision: null },",
      "  sensitivity: { classification: 'private', notice: 'Private test context.' },",
      "  summary: 'A clean consumer contract check.',",
      "  capabilities: [],",
      "  sections: [],",
      "  data: {},",
      "  provenance: { freshness: 'current', sources: [] },",
      "  assumptions: [],",
      "  decisions: [],",
      "  caveats: [],",
      "  omissions: [],",
      "};",
      "const buildContextExport: ContextExportBuilder = async () => contextExport;",
      "const contract = defineInteractiveExportContract<Record<string, never>, { tab: 'overview' }>({",
      "  id: 'briefing', version: 1, sensitivity: { classification: 'private', notice: 'Private working material.' },",
      "  decodeSnapshot(value) {",
      "    if (JSON.stringify(value) !== JSON.stringify({ snapshotData: {}, viewState: { tab: 'overview' } })) throw new Error('invalid capture');",
      "    return { snapshotData: {}, viewState: { tab: 'overview' } };",
      "  },",
      "});",
      "const interactiveExport: InteractiveExportDefinition<Record<string, never>, { tab: 'overview' }> = {",
      "  contract, async buildSnapshot({ signal }) { signal.throwIfAborted(); return { snapshotData: {}, viewState: { tab: 'overview' } }; },",
      "};",
      "const interactiveShell = <AppShell app={villaApp} interactiveExport={interactiveExport}>Interactive content</AppShell>;",
      "const mount = () => mountInteractiveExport({ contract, render: ({ viewState }) => <p>{viewState.tab}</p> });",
      "const interactivePlugin = localWebInteractiveExport({ appId: 'consumer', contract: './contract.ts', entry: './offline.tsx' });",
      "void interactiveShell; void mount; void interactivePlugin; void descriptor;",
      "const contextExportButton = <ContextExportButton buildContextExport={buildContextExport} />;",
      "const renderedContextExport = renderContextExport(contextExport);",
      "const validatedContextExport: LocalWebContextV1 = validateLocalWebContext(contextExport);",
      'const legacyShell = (',
      '  <AppShell',
      '    app={legacyApp}',
      '    navigation={<a href="#navigation">Navigation</a>}',
      '    actions={<button type="button">Action</button>}',
      '  >',
      '    Legacy content',
      '  </AppShell>',
      ');',
      'const conditionalAction: ReactNode = globalThis.Boolean(true)',
      '  ? <button type="button">Conditional action</button>',
      '  : null;',
      'const nullableLegacyShell = (',
      '  <AppShell app={legacyApp} actions={conditionalAction}>',
      '    Nullable legacy content',
      '  </AppShell>',
      ');',
      'const appPageShell = (',
      "  <AppShell app={villaApp} page={{ appHref: '/samplebeta/', label: 'David Platt' }}>",
      '    App page content',
      '  </AppShell>',
      ');',
      'const containedLegacyShell = (',
      '  <AppShell',
      '    app={legacyApp}',
      '    contentMode="contained"',
      '    navigation={<a href="#navigation">Navigation</a>}',
      '    actions={<button type="button">Action</button>}',
      '  >',
      '    Contained legacy content',
      '  </AppShell>',
      ');',
      'const edgeToEdgeLegacyShell = (',
      '  <AppShell',
      '    app={legacyApp}',
      '    contentMode="edge-to-edge"',
      '    navigation={<a href="#navigation">Navigation</a>}',
      '    actions={<button type="button">Action</button>}',
      '  >',
      '    Edge-to-edge legacy content',
      '  </AppShell>',
      ');',
      'const platformShell = (',
      '  <PlatformShell location={platformLocation} contentMode={platformContentMode}>',
      '    Platform content',
      '  </PlatformShell>',
      ');',
      'const themeIcons = themeIconNames.map((name) => <Icon name={name} />);',
      'const contentPrimitives = (',
      '  <>',
      '    <ViewHeader title="Overview" description="Current view" />',
      '    <SectionNav label="View sections"><a href="#overview">Overview</a></SectionNav>',
      '    <MetricGroup><Metric label="Apps" value="4" /></MetricGroup>',
      '    <DataViewport label="App data"><table><tbody><tr><td>Ready</td></tr></tbody></table></DataViewport>',
      '  </>',
      ');',
      'void selectedMode;',
      'void legacyShell;',
      'void nullableLegacyShell;',
      'void appPageShell;',
      'void containedLegacyShell;',
      'void edgeToEdgeLegacyShell;',
      'void platformShell;',
      'void themeIcons;',
      'void contentPrimitives;',
      'void contextExportButton;',
      'void renderedContextExport;',
      'void validatedContextExport;',
      'void readColourMode(window.localStorage);',
      '',
    ].join('\n'),
  );
  await writeFile(
    join(consumerRoot, 'tsconfig.bundler.json'),
    `${JSON.stringify(
      {
        compilerOptions: {
          lib: ['ES2022', 'DOM'],
          module: 'ESNext',
          moduleResolution: 'Bundler',
          jsx: 'react-jsx',
          noEmit: true,
          strict: true,
          target: 'ES2022',
        },
        files: ['src.tsx'],
      },
      null,
      2,
    )}\n`,
  );
  await writeFile(
    join(consumerRoot, 'tsconfig.nodenext.json'),
    `${JSON.stringify(
      {
        compilerOptions: {
          lib: ['ES2022', 'DOM'],
          module: 'NodeNext',
          moduleResolution: 'NodeNext',
          jsx: 'react-jsx',
          noEmit: true,
          strict: true,
          target: 'ES2022',
        },
        files: ['src.tsx'],
      },
      null,
      2,
    )}\n`,
  );

  for (const dependencyName of consumerDependencies) {
    const dependencyRoot = join(repositoryRoot, 'node_modules', ...dependencyName.split('/'));
    const dependencyPackage = JSON.parse(
      await readFile(join(dependencyRoot, 'package.json'), 'utf8'),
    );
    if (dependencyPackage.version !== sourcePackage.devDependencies[dependencyName]) {
      throw new Error(`consumer dependency ${dependencyName} does not match the lockfile install`);
    }
    const dependencyLink = join(consumerRoot, 'node_modules', ...dependencyName.split('/'));
    await mkdir(dirname(dependencyLink), { recursive: true });
    await symlink(dependencyRoot, dependencyLink, process.platform === 'win32' ? 'junction' : 'dir');
  }

  if (!suppliedTarball) run(
    npm,
    ['pack', '--workspace', '@local-web/ui', '--pack-destination', packageDestination],
    repositoryRoot,
    npmEnvironment,
  );
  const [tarballName] = await readdir(packageDestination);
  if (!suppliedTarball && !tarballName?.endsWith('.tgz')) {
    throw new Error('UI package tarball was not created');
  }

  const tarball = suppliedTarball ?? join(packageDestination, tarballName);
  const installArgs = createOfflineTarballInstallArgs(tarball);
  assertOfflineTarballInstall(installArgs, tarball);
  run(npm, installArgs, consumerRoot, npmEnvironment);

  const installedPackageRoot = join(consumerRoot, 'node_modules', '@local-web', 'ui');
  const installedPackage = JSON.parse(
    await readFile(join(installedPackageRoot, 'package.json'), 'utf8'),
  );
  if (installedPackage.version !== '0.7.2') {
    throw new Error('UI package does not expose the current 0.7.2 release');
  }
  if (installedPackage.exports?.['./styles.css'] !== './dist/styles.css') {
    throw new Error('UI package does not expose the explicit styles export');
  }
  if (
    installedPackage.exports?.['./vite']?.types !== './dist/vite.d.ts'
    || installedPackage.exports?.['./vite']?.import !== './dist/vite.js'
  ) {
    throw new Error('UI package does not expose the explicit Vite integration');
  }
  if (installedPackage.dependencies || installedPackage.devDependencies) {
    throw new Error('packed UI package unexpectedly requires install-time dependencies');
  }
  await stat(join(installedPackageRoot, 'dist', 'styles.css'));
  await stat(join(installedPackageRoot, 'dist', 'fallback-theme.css'));
  await stat(join(installedPackageRoot, 'dist', 'vite.d.ts'));
  await stat(join(installedPackageRoot, 'dist', 'vite.js'));
  if (!consumerRoot.includes(' ')) {
    throw new Error('Vite fallback regression must execute below a path containing a space');
  }
  const installedViteModule = pathToFileURL(
    join(installedPackageRoot, 'dist', 'vite.js'),
  ).href;
  const { localWebApp } = await import(installedViteModule);
  const plugin = localWebApp({ appId: 'consumer', basePath: '/' });
  let middleware;
  plugin.configureServer({
    middlewares: { use: (registered) => { middleware = registered; } },
  });
  let fallbackBody;
  middleware(
    { url: '/_local-web/platform/theme.css' },
    { end: (body) => { fallbackBody = body; }, setHeader: () => undefined },
    () => { throw new Error('reserved fallback route was not handled'); },
  );
  if (
    typeof fallbackBody !== 'string'
    || !fallbackBody.includes('--lwp-colour-canvas')
    || fallbackBody.includes('.lwp-app-shell')
    || fallbackBody.includes('.lwp-button')
  ) {
    throw new Error('default fallback route did not serve the dedicated cosmetic theme');
  }

  const declarations = await readFile(join(installedPackageRoot, 'dist', 'index.d.ts'), 'utf8');
  if (declarations.includes('.css') || !declarations.includes("'./colourMode.js'")) {
    throw new Error('UI declarations have an invalid ESM dependency graph');
  }
  const javascript = await readFile(join(installedPackageRoot, 'dist', 'index.js'), 'utf8');
  if (/\b(?:from\s+|import\s+)["']@tabler\//u.test(javascript)) {
    throw new Error('packed UI JavaScript did not bundle its pinned Tabler implementation');
  }

  if ((await readdir(installedPackageRoot)).sort().join(',') !== 'dist,package.json') {
    throw new Error('consumer UI surface must contain only public dist and metadata');
  }
  for (const dependencyName of ['@tabler']) {
    try {
      await stat(join(consumerRoot, 'node_modules', dependencyName));
      throw new Error(`consumer unexpectedly resolved ${dependencyName}`);
    } catch (error) {
      if (error?.code !== 'ENOENT') {
        throw error;
      }
    }
  }

  const tsc = join(consumerRoot, 'node_modules', 'typescript', 'bin', 'tsc');
  await stat(tsc);
  run(process.execPath, [tsc, '--project', 'tsconfig.bundler.json'], consumerRoot);
  run(process.execPath, [tsc, '--project', 'tsconfig.nodenext.json'], consumerRoot);

  await writeFile(join(consumerRoot, 'contract.ts'), `
import { defineInteractiveExportContract } from '@local-web/ui';
export default defineInteractiveExportContract<Record<string, never>, { tab: 'overview' }>({
  id: 'briefing', version: 1,
  sensitivity: { classification: 'private', notice: 'Private working material.' },
  decodeSnapshot(value) {
    if (JSON.stringify(value) !== JSON.stringify({ snapshotData: {}, viewState: { tab: 'overview' } })) throw new Error('invalid capture');
    return { snapshotData: {}, viewState: { tab: 'overview' } };
  },
});
`);
  await writeFile(join(consumerRoot, 'offline.tsx'), `
import { AppShell, mountInteractiveExport } from '@local-web/ui';
import '@local-web/ui/styles.css';
import contract from './contract';
mountInteractiveExport({ contract, render: ({ viewState }) => <AppShell app={{ id: 'consumer', name: 'Consumer', icon: 'apps', accent: '#3859D6' }}><p>{viewState.tab}</p></AppShell> });
`);
  await writeFile(join(consumerRoot, 'hosted.tsx'), `
import { AppShell } from '@local-web/ui';
import { createRoot } from 'react-dom/client';
import '@local-web/ui/styles.css';
import contract from './contract';
const interactiveExport = { contract, async buildSnapshot() { return { snapshotData: {}, viewState: { tab: 'overview' as const } }; } };
createRoot(document.getElementById('root')!).render(<AppShell app={{ id: 'consumer', name: 'Consumer', icon: 'apps', accent: '#3859D6' }} interactiveExport={interactiveExport}>Hosted</AppShell>);
`);
  await writeFile(join(consumerRoot, 'index.html'), '<!doctype html><html><head><title>Consumer</title></head><body><div id="root"></div><script type="module" src="/hosted.tsx"></script></body></html>');
  await writeFile(join(consumerRoot, 'local-web.json'), JSON.stringify({ id: 'consumer' }));
  await writeFile(join(consumerRoot, 'vite.config.ts'), `
import { defineConfig } from 'vite';
import { localWebApp, localWebInteractiveExport } from '@local-web/ui/vite';
export default defineConfig({ plugins: [
  localWebApp({ appId: 'consumer', basePath: '/', accent: '#3859D6' }),
  localWebInteractiveExport({ appId: 'consumer', contract: './contract.ts', entry: './offline.tsx' }),
] });
`);
  run('git', ['init', '-q'], consumerRoot);
  run('git', ['-c', 'user.name=UI Consumer', '-c', 'user.email=consumer@example.invalid',
    '-c', 'core.hooksPath=/dev/null', 'commit', '--allow-empty', '-qm', 'consumer fixture'], consumerRoot);
  run(process.execPath, [join(consumerRoot, 'node_modules', 'vite', 'bin', 'vite.js'), 'build'], consumerRoot);
  const templates = (await readdir(join(consumerRoot, 'dist', 'assets')))
    .filter((name) => /^local-web-interactive-export-[a-f0-9]{64}\.html$/.test(name));
  if (templates.length !== 1) throw new Error('packed consumer did not emit its content-addressed offline template');
  const template = await readFile(join(consumerRoot, 'dist', 'assets', templates[0]), 'utf8');
  if (!template.includes("connect-src 'none'") || template.includes('sourceMappingURL')
    || /<(?:script|link)[^>]+(?:src|href)=/i.test(template)) {
    throw new Error('packed consumer offline template is not self-contained');
  }
  console.log('Packed dist-only consumer: Bundler + NodeNext types and hosted/offline Vite build passed.');
} finally {
  await rm(disposableRoot, { force: true, recursive: true });
}
