// Inline code suggestions for Notebook 7 / JupyterLab 4.1+: registers a provider
// that asks this user's notebook server (jupyterhub_pilot/complete.py).
//
// shortcut: written by hand in the shape JupyterLab expects from a built
// extension, so the package needs no JS build. It must stay one plain file with
// no imports; move to a real labextension build if it needs more.
// Browsers cache this file: when you change it, rename it (remoteEntry.2.js) in
// package.json and pyproject.toml.
(() => {
  const NAME = 'jupyterhub-pilot-complete';
  let scope;

  // A package the page already loaded (JupyterLab shares its own with extensions).
  const shared = async pkg => {
    const versions = Object.values((scope && scope[pkg]) || {});
    const entry = versions.find(v => v.loaded) || versions[0];
    if (!entry) throw new Error(NAME + ': ' + pkg + ' is not shared by this page');
    return (await entry.get())();
  };

  const provider = services => {
    const settings = services.ServerConnection.makeSettings();
    let off = false; // set by the server when suggestions are turned off
    return {
      name: 'JupyterHub-Pilot',
      identifier: NAME,
      // Ask only after a pause in typing; give up after 10 s.
      schema: { default: { debouncerDelay: 700, timeout: 10000 } },
      async fetch(request, context) {
        const none = { items: [] };
        if (off || !/^text\/x-i?python$/.test(request.mimeType || '')) return none;
        // Code cells above the current one, so the model knows the variables.
        const nb = context && context.widget && context.widget.content;
        let above = '';
        if (nb && nb.widgets && nb.activeCellIndex > 0) {
          above = nb.widgets
            .slice(0, nb.activeCellIndex)
            .filter(c => c.model.type === 'code')
            .map(c => c.model.sharedModel.getSource())
            .join('\n\n') + '\n\n';
        }
        const body = {
          prefix: (above + request.text.slice(0, request.offset)).slice(-2000),
          suffix: request.text.slice(request.offset, request.offset + 500)
        };
        try {
          const reply = await services.ServerConnection.makeRequest(
            settings.baseUrl + 'jupyterhub-pilot/complete',
            { method: 'POST', body: JSON.stringify(body) },
            settings
          );
          if (!reply.ok) return none;
          const data = await reply.json();
          off = !!data.off;
          return data.text ? { items: [{ insertText: data.text }] } : none;
        } catch (e) {
          return none;
        }
      }
    };
  };

  window._JUPYTERLAB = window._JUPYTERLAB || {};
  window._JUPYTERLAB[NAME] = {
    init: s => {
      scope = s;
    },
    get: async () => {
      const [completer, services] = await Promise.all([
        shared('@jupyterlab/completer'),
        shared('@jupyterlab/services')
      ]);
      const plugin = {
        id: NAME + ':plugin',
        autoStart: true,
        requires: [completer.ICompletionProviderManager],
        activate: (app, manager) => manager.registerInlineProvider(provider(services))
      };
      return () => ({ __esModule: true, default: plugin });
    }
  };
})();
