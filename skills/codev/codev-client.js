/** Codev browser data contract v1. Keep this adapter local so it can be replaced. */
export class CodevError extends Error {
  constructor(status, detail) {
    super(detail?.message || 'The app request did not complete.');
    this.name = 'CodevError';
    this.status = status;
    this.code = detail?.code || 'request_failed';
    this.detail = detail;
  }
}

const segment = (value) => encodeURIComponent(value);
const base64url = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes)))
  .replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');

export class CodevClient {
  constructor({ fetch: transport = globalThis.fetch.bind(globalThis), storage = globalThis.sessionStorage } = {}) {
    this.fetch = transport;
    this.storage = storage;
    this.config = null;
    this.session = { authenticated: false, csrf: 'codev' };
  }

  async request(path, { method = 'GET', body, key, revision, signal, raw = false } = {}) {
    if (!path.startsWith('/.codev/')) throw new Error('Use a local Codev app endpoint.');
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (method !== 'GET') headers['X-Codev-CSRF'] = this.session.csrf || 'codev';
    if (this.config?.digest) headers['X-Codev-Contract'] = this.config.digest;
    if (key) headers['Idempotency-Key'] = key;
    if (revision !== undefined) headers['If-Match'] = `"${revision}"`;
    let response;
    try {
      response = await this.fetch(path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal,
      });
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new CodevError(0, { code: 'network_error', message: 'Connection lost. Keep your input and retry with the same operation key.' });
    }
    if (!response.ok) {
      let detail;
      try { detail = (await response.json()).detail; } catch { /* A failed gateway may return plain text. */ }
      throw new CodevError(response.status, detail);
    }
    return raw ? response : response.status === 204 ? undefined : response.json();
  }

  async ready() {
    this.config = null;
    await this.completeSignIn();
    this.session = await this.request('/.codev/session');
    this.config = await this.request('/.codev/config');
    return { config: this.config, session: this.session };
  }

  async signIn() {
    const verifier = base64url(crypto.getRandomValues(new Uint8Array(32)));
    const state = base64url(crypto.getRandomValues(new Uint8Array(32)));
    const challenge = base64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
    const login = await this.request('/.codev/session/start', {
      method: 'POST', body: { verifier: undefined, challenge, state, return_path: location.pathname + location.search },
    });
    this.storage.setItem('codev.login', JSON.stringify({ verifier, state, request_id: login.request_id, created_at: Date.now() }));
    location.assign(login.approval_url);
  }

  async completeSignIn() {
    if (typeof location === 'undefined') return false;
    const fragment = new URLSearchParams(location.hash.slice(1));
    if (!fragment.has('codev_code')) return false;
    const saved = this.storage.getItem('codev.login');
    // Remove the one-time code from browser history before making another request.
    history.replaceState(history.state, '', location.pathname + location.search);
    const login = saved ? JSON.parse(saved) : null;
    if (!login || Date.now() - login.created_at > 300000 || login.state !== fragment.get('codev_state') || login.request_id !== fragment.get('codev_request')) {
      throw new CodevError(400, { code: 'invalid_login_state', message: 'Start sign-in again from this app tab.' });
    }
    this.session = await this.request('/.codev/session/exchange', {
      method: 'POST', body: { code: fragment.get('codev_code'), verifier: login.verifier, state: login.state, request_id: login.request_id },
    });
    this.storage.removeItem('codev.login');
    return true;
  }

  async signOut() {
    await this.request('/.codev/session', { method: 'DELETE' });
    this.session = { authenticated: false, csrf: 'codev' };
  }

  collection(name) {
    const path = '/.codev/data/' + segment(name);
    return {
      list: ({ limit = 50, after, filter, signal } = {}) => {
        const query = new URLSearchParams({ limit: String(limit) });
        if (after) query.set('after', after);
        if (filter) query.set('filter', JSON.stringify(filter));
        return this.request(path + '?' + query, { signal });
      },
      get: (id, { signal } = {}) => this.request(path + '/' + segment(id), { signal }),
      create: (data, { key, signal } = {}) => {
        if (!key) throw new Error('Keep one operation key per create and reuse it on retry.');
        return this.request(path, { method: 'POST', body: { data }, key, signal });
      },
      patch: (id, changes, { revision, signal } = {}) => {
        if (!Number.isSafeInteger(revision) || revision < 1) throw new Error('Supply the record revision before saving.');
        return this.request(path + '/' + segment(id), { method: 'PATCH', body: changes, revision, signal });
      },
      delete: (id, { revision, signal } = {}) => {
        if (!Number.isSafeInteger(revision) || revision < 1) throw new Error('Supply the record revision before deleting.');
        return this.request(path + '/' + segment(id), { method: 'DELETE', revision, signal });
      },
      history: (id) => this.request(path + '/' + segment(id) + '/history'),
      restore: (id, historicalRevision, { revision } = {}) => {
        if (!Number.isSafeInteger(revision) || revision < 1) throw new Error('Supply the current revision before restoring.');
        return this.request(path + '/' + segment(id) + '/restore', { method: 'POST', revision, body: { revision: historicalRevision } });
      },
    };
  }

  invoke(route, body, { signal, query, stream = false } = {}) {
    const suffix = query ? '?' + new URLSearchParams(query) : '';
    return this.request('/.codev/api/' + segment(route) + suffix, { method: 'POST', body, signal, raw: stream });
  }
}
