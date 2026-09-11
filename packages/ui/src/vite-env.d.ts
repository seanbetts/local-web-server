declare module '*.css';
declare module 'virtual:local-web-interactive-export' {
  const descriptor: import('./interactiveExportModel.js').InteractiveExportTemplateDescriptor;
  export default descriptor;
}
